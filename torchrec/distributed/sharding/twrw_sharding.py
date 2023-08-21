#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import itertools
import math
from typing import Any, cast, Dict, List, Optional, Tuple, TypeVar

import torch
import torch.distributed as dist
from torchrec.distributed.comm import get_local_size, intra_and_cross_node_pg
from torchrec.distributed.dist_data import (
    KJTAllToAll,
    PooledEmbeddingsAllToAll,
    PooledEmbeddingsReduceScatter,
)
from torchrec.distributed.embedding_lookup import GroupedPooledEmbeddingsLookup
from torchrec.distributed.embedding_sharding import (
    BaseEmbeddingDist,
    BaseEmbeddingLookup,
    BaseSparseFeaturesDist,
    bucketize_kjt_before_all2all,
    EmbeddingSharding,
    EmbeddingShardingContext,
    EmbeddingShardingInfo,
    group_tables,
)
from torchrec.distributed.embedding_types import (
    BaseGroupedFeatureProcessor,
    EmbeddingComputeKernel,
    GroupedEmbeddingConfig,
    ShardedEmbeddingTable,
)
from torchrec.distributed.types import (
    Awaitable,
    CommOp,
    QuantizedCommCodecs,
    ShardedTensorMetadata,
    ShardingEnv,
    ShardMetadata,
)
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor
from torchrec.streamable import Multistreamable

C = TypeVar("C", bound=Multistreamable)
F = TypeVar("F", bound=Multistreamable)
T = TypeVar("T")
W = TypeVar("W")


class BaseTwRwEmbeddingSharding(EmbeddingSharding[C, F, T, W]):
    """
    Base class for table wise row wise sharding.
    """

    def __init__(
        self,
        sharding_infos: List[EmbeddingShardingInfo],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
        need_pos: bool = False,
        qcomm_codecs_registry: Optional[Dict[str, QuantizedCommCodecs]] = None,
    ) -> None:
        super().__init__(qcomm_codecs_registry=qcomm_codecs_registry)
        self._env = env
        self._pg: Optional[dist.ProcessGroup] = self._env.process_group
        self._world_size: int = self._env.world_size
        self._rank: int = self._env.rank
        self._device = device
        self._need_pos = need_pos
        intra_pg, cross_pg = intra_and_cross_node_pg(
            device, backend=dist.get_backend(self._pg)
        )
        self._intra_pg: Optional[dist.ProcessGroup] = intra_pg
        self._cross_pg: Optional[dist.ProcessGroup] = cross_pg
        self._local_size: int = (
            intra_pg.size() if intra_pg else get_local_size(self._world_size)
        )

        sharded_tables_per_rank = self._shard(sharding_infos)
        self._grouped_embedding_configs_per_rank: List[
            List[GroupedEmbeddingConfig]
        ] = []
        self._grouped_embedding_configs_per_node: List[
            List[GroupedEmbeddingConfig]
        ] = []
        self._grouped_embedding_configs_per_rank = group_tables(sharded_tables_per_rank)
        self._grouped_embedding_configs_per_node = [
            self._grouped_embedding_configs_per_rank[rank]
            for rank in range(self._world_size)
            if rank % self._local_size == 0
        ]
        self._has_feature_processor: bool = False
        for group_config in self._grouped_embedding_configs_per_rank[
            self._rank // self._local_size
        ]:
            if group_config.has_feature_processor:
                self._has_feature_processor = True

    def _shard(
        self,
        sharding_infos: List[EmbeddingShardingInfo],
    ) -> List[List[ShardedEmbeddingTable]]:
        world_size = self._world_size
        local_size = self._local_size
        tables_per_rank: List[List[ShardedEmbeddingTable]] = [
            [] for i in range(world_size)
        ]
        for info in sharding_infos:
            # pyre-ignore [16]
            table_node = info.param_sharding.ranks[0] // local_size
            # pyre-fixme [16]
            shards = info.param_sharding.sharding_spec.shards

            # construct the global sharded_tensor_metadata
            global_metadata = ShardedTensorMetadata(
                shards_metadata=shards,
                size=torch.Size(
                    [
                        info.embedding_config.num_embeddings,
                        info.embedding_config.embedding_dim,
                    ]
                ),
            )

            for rank in range(
                table_node * local_size,
                (table_node + 1) * local_size,
            ):
                rank_idx = rank - (table_node * local_size)
                tables_per_rank[rank].append(
                    ShardedEmbeddingTable(
                        num_embeddings=info.embedding_config.num_embeddings,
                        embedding_dim=info.embedding_config.embedding_dim,
                        name=info.embedding_config.name,
                        embedding_names=info.embedding_config.embedding_names,
                        data_type=info.embedding_config.data_type,
                        feature_names=info.embedding_config.feature_names,
                        pooling=info.embedding_config.pooling,
                        is_weighted=info.embedding_config.is_weighted,
                        has_feature_processor=info.embedding_config.has_feature_processor,
                        local_rows=shards[rank_idx].shard_sizes[0],
                        local_cols=info.embedding_config.embedding_dim,
                        compute_kernel=EmbeddingComputeKernel(
                            info.param_sharding.compute_kernel
                        ),
                        local_metadata=shards[rank_idx],
                        global_metadata=global_metadata,
                        weight_init_max=info.embedding_config.weight_init_max,
                        weight_init_min=info.embedding_config.weight_init_min,
                        fused_params=info.fused_params,
                    )
                )

        return tables_per_rank

    def embedding_dims(self) -> List[int]:
        embedding_dims = []
        for grouped_embedding_configs in self._grouped_embedding_configs_per_node:
            for grouped_config in grouped_embedding_configs:
                embedding_dims.extend(grouped_config.embedding_dims())
        return embedding_dims

    def embedding_names(self) -> List[str]:
        embedding_names = []
        for grouped_embedding_configs in self._grouped_embedding_configs_per_node:
            for grouped_config in grouped_embedding_configs:
                embedding_names.extend(grouped_config.embedding_names())
        return embedding_names

    def embedding_names_per_rank(self) -> List[List[str]]:
        raise NotImplementedError

    def embedding_shard_metadata(self) -> List[Optional[ShardMetadata]]:
        embedding_shard_metadata = []
        for grouped_config in self._grouped_embedding_configs_per_node:
            for config in grouped_config:
                embedding_shard_metadata.extend(config.embedding_shard_metadata())
        return embedding_shard_metadata

    def feature_names(self) -> List[str]:
        feature_names = []
        for grouped_config in self._grouped_embedding_configs_per_node:
            for config in grouped_config:
                feature_names.extend(config.feature_names())
        return feature_names

    def _get_feature_hash_sizes(self) -> List[int]:
        feature_hash_sizes: List[int] = []
        for grouped_config in self._grouped_embedding_configs_per_node:
            for config in grouped_config:
                feature_hash_sizes.extend(config.feature_hash_sizes())
        return feature_hash_sizes

    def _dim_sum_per_node(self) -> List[int]:
        dim_sum_per_rank = []
        for grouped_embedding_configs in self._grouped_embedding_configs_per_node:
            dim_sum = 0
            for grouped_config in grouped_embedding_configs:
                dim_sum += grouped_config.dim_sum()
            dim_sum_per_rank.append(dim_sum)
        return dim_sum_per_rank

    def _features_per_rank(
        self, group: List[List[GroupedEmbeddingConfig]]
    ) -> List[int]:
        features_per_rank = []
        for grouped_embedding_configs in group:
            num_features = 0
            for grouped_config in grouped_embedding_configs:
                num_features += grouped_config.num_features()
            features_per_rank.append(num_features)
        return features_per_rank


class TwRwSparseFeaturesDist(BaseSparseFeaturesDist[KeyedJaggedTensor]):
    """
    Bucketizes sparse features in TWRW fashion and then redistributes with an AlltoAll
    collective operation.

    Args:
        pg (dist.ProcessGroup): ProcessGroup for AlltoAll communication.
        intra_pg (dist.ProcessGroup): ProcessGroup within single host group for AlltoAll
            communication.
        id_list_features_per_rank (List[int]): number of id list features to send to
            each rank.
        id_score_list_features_per_rank (List[int]): number of id score list features to
            send to each rank.
        id_list_feature_hash_sizes (List[int]): hash sizes of id list features.
        id_score_list_feature_hash_sizes (List[int]): hash sizes of id score list
            features.
        device (Optional[torch.device]): device on which buffers will be allocated.
        has_feature_processor (bool): existence of a feature processor (ie. position
            weighted features).

    Example::

        3 features
        2 hosts with 2 devices each

        Bucketize each feature into 2 buckets
        Staggered shuffle with feature splits [2, 1]
        AlltoAll operation

        NOTE: result of staggered shuffle and AlltoAll operation look the same after
        reordering in AlltoAll

        Result:
            host 0 device 0:
                feature 0 bucket 0
                feature 1 bucket 0

            host 0 device 1:
                feature 0 bucket 1
                feature 1 bucket 1

            host 1 device 0:
                feature 2 bucket 0

            host 1 device 1:
                feature 2 bucket 1
    """

    def __init__(
        self,
        pg: dist.ProcessGroup,
        local_size: int,
        features_per_rank: List[int],
        feature_hash_sizes: List[int],
        device: Optional[torch.device] = None,
        has_feature_processor: bool = False,
        need_pos: bool = False,
    ) -> None:
        super().__init__()
        assert pg.size() % local_size == 0, "currently group granularity must be node"

        self._world_size: int = pg.size()
        self._local_size: int = local_size
        self._num_cross_nodes: int = self._world_size // self._local_size
        feature_block_sizes = [
            math.ceil(hash_size / self._local_size) for hash_size in feature_hash_sizes
        ]

        self._sf_staggered_shuffle: List[int] = self._staggered_shuffle(
            features_per_rank
        )
        self.register_buffer(
            "_feature_block_sizes_tensor",
            torch.tensor(
                feature_block_sizes,
                device=device,
                dtype=torch.int32,
            ),
        )
        self.register_buffer(
            "_sf_staggered_shuffle_tensor",
            torch.tensor(
                self._sf_staggered_shuffle,
                device=device,
                dtype=torch.int32,
            ),
        )
        self._dist = KJTAllToAll(
            pg=pg,
            splits=features_per_rank,
            stagger=self._num_cross_nodes,
        )
        self._has_feature_processor = has_feature_processor
        self._need_pos = need_pos

    def forward(
        self,
        sparse_features: KeyedJaggedTensor,
    ) -> Awaitable[Awaitable[KeyedJaggedTensor]]:
        """
        Bucketizes sparse feature values into local world size number of buckets,
        performs staggered shuffle on the sparse features, and then performs AlltoAll
        operation.

        Args:
            sparse_features (KeyedJaggedTensor): sparse features to bucketize and
                redistribute.

        Returns:
            Awaitable[KeyedJaggedTensor]: awaitable of KeyedJaggedTensor.
        """

        if sparse_features.variable_stride_per_key():
            raise ValueError(
                "Variable batch per feature is not supported with table-wise-row-wise sharding"
            )

        bucketized_features = bucketize_kjt_before_all2all(
            sparse_features,
            num_buckets=self._local_size,
            block_sizes=self._feature_block_sizes_tensor,
            output_permute=False,
            bucketize_pos=self._has_feature_processor
            if sparse_features.weights_or_none() is None
            else self._need_pos,
        )[0].permute(
            self._sf_staggered_shuffle,
            self._sf_staggered_shuffle_tensor,
        )

        return self._dist(bucketized_features)

    def _staggered_shuffle(self, features_per_rank: List[int]) -> List[int]:
        """
        Reorders sparse data such that data is in contiguous blocks and correctly
        ordered for global TWRW layout.
        """

        nodes = self._world_size // self._local_size
        features_per_node = [
            features_per_rank[node * self._local_size] for node in range(nodes)
        ]
        node_offsets = [0] + list(itertools.accumulate(features_per_node))
        num_features = node_offsets[-1]

        return [
            bucket * num_features + feature
            for node in range(nodes)
            for bucket in range(self._local_size)
            for feature in range(node_offsets[node], node_offsets[node + 1])
        ]


class TwRwPooledEmbeddingDist(
    BaseEmbeddingDist[EmbeddingShardingContext, torch.Tensor, torch.Tensor]
):
    """
    Redistributes pooled embedding tensor in TWRW fashion by performing a reduce-scatter
    operation row wise on the host level and then an AlltoAll operation table wise on
    the global level.

    Args:
        cross_pg (dist.ProcessGroup): global level ProcessGroup for AlltoAll
            communication.
        intra_pg (dist.ProcessGroup): host level ProcessGroup for reduce-scatter
            communication.
        dim_sum_per_node (List[int]): number of features (sum of dimensions) of the
            embedding for each host.
        device (Optional[torch.device]): device on which buffers will be allocated.
    """

    def __init__(
        self,
        rank: int,
        cross_pg: dist.ProcessGroup,
        intra_pg: dist.ProcessGroup,
        dim_sum_per_node: List[int],
        device: Optional[torch.device] = None,
        qcomm_codecs_registry: Optional[Dict[str, QuantizedCommCodecs]] = None,
    ) -> None:
        super().__init__()
        self._rank = rank
        self._intra_pg: dist.ProcessGroup = intra_pg
        self._cross_pg: dist.ProcessGroup = cross_pg

        self._intra_dist = PooledEmbeddingsReduceScatter(
            intra_pg,
            codecs=qcomm_codecs_registry.get(
                CommOp.POOLED_EMBEDDINGS_REDUCE_SCATTER.name, None
            )
            if qcomm_codecs_registry
            else None,
        )
        self._cross_dist = PooledEmbeddingsAllToAll(
            cross_pg,
            dim_sum_per_node,
            device,
            codecs=qcomm_codecs_registry.get(
                CommOp.POOLED_EMBEDDINGS_ALL_TO_ALL.name, None
            )
            if qcomm_codecs_registry
            else None,
        )

    def forward(
        self,
        local_embs: torch.Tensor,
        sharding_ctx: Optional[EmbeddingShardingContext] = None,
    ) -> Awaitable[torch.Tensor]:
        """
        Performs reduce-scatter pooled operation on pooled embeddings tensor followed by
        AlltoAll pooled operation.

        Args:
            local_embs (torch.Tensor): pooled embeddings tensor to distribute.

        Returns:
            Awaitable[torch.Tensor]: awaitable of pooled embeddings tensor.
        """
        if sharding_ctx is not None and len(set(sharding_ctx.batch_size_per_rank)) > 1:
            # preprocess batch_size_per_rank
            (
                batch_size_per_rank_by_cross_group,
                batch_size_sum_by_cross_group,
            ) = self._preprocess_batch_size_per_rank(
                self._intra_pg.size(),
                self._cross_pg.size(),
                sharding_ctx.batch_size_per_rank,
            )
            # Perform ReduceScatterV within one host
            lengths = batch_size_sum_by_cross_group
            local_rank = self._rank % self._intra_pg.size()
            batch_size_per_rank = batch_size_per_rank_by_cross_group[local_rank]
            rs_result = self._intra_dist(local_embs, input_splits=lengths).wait()
            return self._cross_dist(
                rs_result,
                batch_size_per_rank=batch_size_per_rank,
                batch_size_per_feature_pre_a2a=sharding_ctx.batch_size_per_feature_pre_a2a,
            )
        elif sharding_ctx is not None:
            if local_embs.numel() == 0 and local_embs.shape == torch.Size([0]):
                assert len(set(sharding_ctx.batch_size_per_feature_pre_a2a)) == 1
                b_local = sharding_ctx.batch_size_per_feature_pre_a2a[0]
                local_embs = local_embs.view(
                    b_local * self._intra_pg.size() * self._cross_pg.size(), 0
                )
            return self._cross_dist(self._intra_dist(local_embs).wait())
        else:
            return self._cross_dist(self._intra_dist(local_embs).wait())

    def _preprocess_batch_size_per_rank(
        self, local_size: int, nodes: int, batch_size_per_rank: List[int]
    ) -> Tuple[List[List[int]], List[int]]:
        """
        Reorders `batch_size_per_rank` so it's aligned with reordered features after
        AlltoAll.
        """
        batch_size_per_rank_by_cross_group: List[List[int]] = []
        batch_size_sum_by_cross_group: List[int] = []
        for local_rank in range(local_size):
            batch_size_per_rank_: List[int] = []
            batch_size_sum = 0
            for node in range(nodes):
                batch_size_per_rank_.append(
                    batch_size_per_rank[local_rank + node * local_size]
                )
                batch_size_sum += batch_size_per_rank[local_rank + node * local_size]
            batch_size_per_rank_by_cross_group.append(batch_size_per_rank_)
            batch_size_sum_by_cross_group.append(batch_size_sum)

        return batch_size_per_rank_by_cross_group, batch_size_sum_by_cross_group


class TwRwPooledEmbeddingSharding(
    BaseTwRwEmbeddingSharding[
        EmbeddingShardingContext, KeyedJaggedTensor, torch.Tensor, torch.Tensor
    ]
):
    """
    Shards embedding bags table-wise then row-wise.
    """

    def create_input_dist(
        self, device: Optional[torch.device] = None
    ) -> BaseSparseFeaturesDist[KeyedJaggedTensor]:
        features_per_rank = self._features_per_rank(
            self._grouped_embedding_configs_per_rank
        )
        feature_hash_sizes = self._get_feature_hash_sizes()
        assert self._pg is not None
        assert self._intra_pg is not None
        return TwRwSparseFeaturesDist(
            pg=self._pg,
            local_size=self._intra_pg.size(),
            features_per_rank=features_per_rank,
            feature_hash_sizes=feature_hash_sizes,
            device=device if device is not None else self._device,
            has_feature_processor=self._has_feature_processor,
            need_pos=self._need_pos,
        )

    def create_lookup(
        self,
        device: Optional[torch.device] = None,
        fused_params: Optional[Dict[str, Any]] = None,
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
    ) -> BaseEmbeddingLookup:
        return GroupedPooledEmbeddingsLookup(
            grouped_configs=self._grouped_embedding_configs_per_rank[self._rank],
            pg=self._pg,
            device=device if device is not None else self._device,
            feature_processor=feature_processor,
        )

    def create_output_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseEmbeddingDist[EmbeddingShardingContext, torch.Tensor, torch.Tensor]:
        return TwRwPooledEmbeddingDist(
            rank=self._rank,
            cross_pg=cast(dist.ProcessGroup, self._cross_pg),
            intra_pg=cast(dist.ProcessGroup, self._intra_pg),
            dim_sum_per_node=self._dim_sum_per_node(),
            device=device if device is not None else self._device,
            qcomm_codecs_registry=self.qcomm_codecs_registry,
        )
