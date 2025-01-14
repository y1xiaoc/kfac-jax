# Copyright 2022 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""K-FAC for accumulating statistics."""
from typing import Any, Optional, Generic

import chex
import jax
import jax.numpy as jnp

from kfac_jax._src.utils import misc
from kfac_jax._src.utils import parallel
from kfac_jax._src.utils import types

PyTree = types.PyTree
TPyTree = types.TPyTree


@misc.pytree_dataclass
class WeightedMovingAverage(Generic[TPyTree]):
  """A wrapped class for an arbitrary weighted moving average."""
  weight: chex.Array
  raw_value: TPyTree

  @property
  def value(self) -> TPyTree:
    """The value of the underlying arrays data structure."""
    return jax.tree_util.tree_map(lambda x: x / self.weight, self.raw_value)

  def update(
      self,
      value: TPyTree,
      old_weight_multiplier: chex.Numeric,
      new_weight: chex.Numeric,
  ) -> None:
    """Updates the underlying array and weight accordingly."""
    self.weight = self.weight * old_weight_multiplier + new_weight
    self.raw_value = jax.tree_util.tree_map(
        lambda x, y: x * old_weight_multiplier + y * new_weight,
        self.raw_value,
        value,
    )

  def sync(self, pmap_axis_name: Optional[str]) -> None:
    """Syncs the underlying array across devices."""
    self.raw_value = parallel.pmean_if_pmap(self.raw_value, pmap_axis_name)

  @classmethod
  def zero(cls, shape: chex.Shape) -> "WeightedMovingAverage":
    """Initializes a `WeightedMovingAverage` with a single array of zeros."""
    return WeightedMovingAverage(
        weight=jnp.zeros([]), raw_value=jnp.zeros(shape))

  @classmethod
  def zeros_like(cls, value: PyTree) -> "WeightedMovingAverage":
    """Initializes a `WeightedMovingAverage` with zeros structure like `value`."""
    return WeightedMovingAverage(
        weight=jnp.zeros([]),
        raw_value=jax.tree_util.tree_map(jnp.zeros_like, value)
    )

  def copy(self):
    """Returns a copy of the PyTree structure (but not the JAX arrays)."""
    (flattened, structure) = jax.tree_util.tree_flatten(self)
    return jax.tree_util.tree_unflatten(structure, flattened)


class MultiChunkAccumulator(Generic[TPyTree]):
  """Statistics accumulation, abstracted over multiple chunks."""

  def __init__(
      self,
      init_obj_value: Optional[TPyTree],
      weight: chex.Numeric,
      multi_device: bool,
  ):
    """Initializes an accumulator instance with the provided object and counter.

    Args:
      init_obj_value: The initial value of the accumulator.
      weight: The initial weight, which specifies how many samples are assumed
        to have been already counted in the initial value of the accumulator.
      multi_device: Whether the objects that are accumulated are outputs of a
        multi-device computation (e.g. `jax.pmap`).
    """
    self._accumulator = init_obj_value
    self._weight = weight
    self._multi_device = multi_device

  @property
  def accumulator(self) -> TPyTree:
    """The current value of the underlying not-normalized accumulator."""
    return self._accumulator

  @property
  def weight(self) -> chex.Numeric:
    """The current normalization weight of the underlying accumulator."""
    return self._weight

  @property
  def multi_device(self) -> bool:
    """Whether the accumulator is the output of a multi-device computation."""
    return self._multi_device

  @property
  def value(self) -> TPyTree:
    """The current normalized value of the accumulator."""

    if types.tree_is_empty(self.accumulator):
      return self.accumulator

    if self._multi_device:
      return parallel.pmap_sync_and_divide_value(self.accumulator, self.weight)
    else:
      return parallel.jit_sync_and_divide_value(self.accumulator, self.weight)

  def clear(self) -> None:
    """Sets the underlying accumulator and weight to `None`."""
    self._accumulator = None
    self._weight = None

  def value_and_clear(self) -> TPyTree:
    """Retrieves the normalized value of the accumulator and clears it."""
    value = self.value
    self.clear()
    return value

  def add(self, value_obj: TPyTree, weight: chex.Numeric = 1):
    """Adds an element to the moving average and the max.

    The exact update equation for the statistics are:
      raw_value_t = raw_value_{t-1} + value_obj * weight
      weight_t = weight_{t-1} + weight

    Args:
      value_obj: The value of the object, which scaled by `weight` will be added
        to the accumulator.
      weight: The relative weight of the `value_obj`.
    """
    value_obj = jax.tree_util.tree_map(lambda x: x * weight, value_obj)

    if self._accumulator is None:
      self._accumulator = value_obj
      if isinstance(weight, types.CHEX_SCALAR_TYPES):
        self._weight = jnp.full_like(self._weight, weight)
      elif not isinstance(weight, jax.Array):
        raise ValueError("`weight` should be an instance of float, int or "
                         "jax.Array.")
      elif self._weight.shape != weight.shape:
        raise ValueError("If `weight` is an `jnp.ndarray` then should have the "
                         "same shape as the weight of the accumulator.")
      else:
        self._weight = weight
      return

    if not types.tree_is_empty(self._accumulator):
      if types.tree_is_empty(value_obj):
        raise ValueError("The provided `value_obj` has an empty PyTree "
                         "structure, but the accumulator has been initialized "
                         "with a non-empty PyTree object.")
      self._accumulator = jax.tree_util.tree_map(
          jnp.add, self._accumulator, value_obj)
    elif not types.tree_is_empty(value_obj):
      raise ValueError("The provided `value_obj` has a non-empty PyTree "
                       "structure, but the accumulator has been initialized "
                       "with an empty PyTree object.")
    self._weight = self._weight + weight

  @classmethod
  def zeros_like(
      cls,
      obj: TPyTree,
      multi_device: bool
  ) -> "MultiChunkAccumulator[TPyTree]":
    """Creates a zero initialized accumulator as `obj`."""

    if multi_device:
      value = (parallel.pmap_zeros_like(obj)
               if not types.tree_is_empty(obj) else obj)
      weight = parallel.replicate_all_local_devices(
          jnp.zeros([], dtype=jnp.int32))
    else:
      value = (parallel.jit_zeros_like(obj)
               if not types.tree_is_empty(obj) else obj)
      weight = jnp.zeros([], dtype=jnp.int32)

    return cls(value, weight, multi_device)

  @classmethod
  def empty(cls, multi_device: bool) -> "MultiChunkAccumulator[Any]":
    """Creates an empty accumulator."""

    weight = jnp.zeros([], dtype=jnp.int32)

    if multi_device:
      weight = parallel.replicate_all_local_devices(weight)

    return cls(None, weight, multi_device)

  def __repr__(self):
    return (f"{self.__class__.__name__}({self._accumulator!r}, "
            f"{self._weight!r}, {self._multi_device})")

  def copy(self):
    """Returns a copy of the PyTree structure (but not the JAX arrays)."""
    (flattened, structure) = jax.tree_util.tree_flatten(self)
    return jax.tree_util.tree_unflatten(structure, flattened)


jax.tree_util.register_pytree_node(
    MultiChunkAccumulator,
    lambda x: ((x.accumulator, x.weight), (x.multi_device,)),
    lambda fixed, arrays: MultiChunkAccumulator(*arrays, *fixed)
)
