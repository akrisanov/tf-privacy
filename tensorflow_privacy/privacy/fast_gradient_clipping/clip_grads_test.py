# Copyright 2022, The TensorFlow Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools
from typing import Any, Callable, Dict, List, Optional, Text, Tuple, Union

from absl.testing import parameterized
import tensorflow as tf

from tensorflow_privacy.privacy.fast_gradient_clipping import clip_grads
from tensorflow_privacy.privacy.fast_gradient_clipping import layer_registry


# ==============================================================================
# Type aliases
# ==============================================================================
LayerGenerator = Callable[[int, int], tf.keras.layers.Layer]

ModelGenerator = Callable[
    [LayerGenerator, Union[int, List[int]], int], tf.keras.Model
]


# ==============================================================================
# Helper functions and classes.
# ==============================================================================
class DoubleDense(tf.keras.layers.Layer):
  """Generates two dense layers nested together."""

  def __init__(self, units: int):
    super().__init__()
    self.dense1 = tf.keras.layers.Dense(units)
    self.dense2 = tf.keras.layers.Dense(1)

  def call(self, inputs: Any):
    x = self.dense1(inputs)
    return self.dense2(x)


def double_dense_layer_computation(
    layer_instance: tf.keras.layers.Layer,
    input_args: Tuple[Any, ...],
    input_kwargs: Dict[Text, Any],
    tape: tf.GradientTape,
    num_microbatches: Optional[int],
) -> layer_registry.RegistryFunctionOutput:
  """Layer registry function for the custom `DoubleDense` layer class."""
  vars1, outputs, sqr_norm_fn1 = layer_registry.dense_layer_computation(
      layer_instance.dense1, input_args, input_kwargs, tape, num_microbatches
  )
  vars2, outputs, sqr_norm_fn2 = layer_registry.dense_layer_computation(
      layer_instance.dense2, (outputs,), {}, tape, num_microbatches
  )

  def sqr_norm_fn(base_vars):
    norms1 = sqr_norm_fn1(base_vars[0])
    norms2 = sqr_norm_fn2(base_vars[1])
    return norms1 + norms2

  return [vars1, vars2], outputs, sqr_norm_fn


def compute_true_gradient_norms(
    input_model: tf.keras.Model,
    x_batch: tf.Tensor,
    y_batch: tf.Tensor,
    num_microbatches: Optional[int],
) -> layer_registry.OutputTensor:
  """Computes the real gradient norms for an input `(model, x, y)`."""
  loss_config = input_model.loss.get_config()
  loss_config['reduction'] = tf.keras.losses.Reduction.NONE
  per_example_loss_fn = input_model.loss.from_config(loss_config)
  with tf.GradientTape(persistent=True) as tape:
    y_pred = input_model(x_batch)
    loss = per_example_loss_fn(y_batch, y_pred)
    if num_microbatches is not None:
      loss = tf.reduce_mean(
          tf.reshape(
              loss,
              tf.concat([[num_microbatches, -1], tf.shape(loss)[1:]], axis=0),
          ),
          axis=1,
      )
    if isinstance(loss, tf.RaggedTensor):
      loss = loss.to_tensor()
  sqr_norms = []
  for var in input_model.trainable_variables:
    jacobian = tape.jacobian(loss, var, experimental_use_pfor=False)
    reduction_axes = tf.range(1, len(jacobian.shape))
    sqr_norm = tf.reduce_sum(tf.square(jacobian), axis=reduction_axes)
    sqr_norms.append(sqr_norm)
  sqr_norm_tsr = tf.stack(sqr_norms, axis=1)
  return tf.sqrt(tf.reduce_sum(sqr_norm_tsr, axis=1))


def get_computed_and_true_norms(
    model_generator: ModelGenerator,
    layer_generator: LayerGenerator,
    input_dims: Union[int, List[int]],
    output_dim: int,
    num_microbatches: Optional[int],
    is_eager: bool,
    x_input: tf.Tensor,
    rng_seed: int = 777,
    registry: layer_registry.LayerRegistry = None,
) -> Tuple[tf.Tensor, tf.Tensor]:
  """Obtains the true and computed gradient norms for a model and batch input.

  Helpful testing wrapper function used to avoid code duplication.

  Args:
    model_generator: A function which takes in three arguments:
      `layer_generator`, `idim`, and `odim`. Returns a `tf.keras.Model` that
      accepts input tensors of dimension `idim` and returns output tensors of
      dimension `odim`. Layers of the model are based on the `layer_generator`
      (see below for its description).
    layer_generator: A function which takes in two arguments: `idim` and `odim`.
      Returns a `tf.keras.layers.Layer` that accepts input tensors of dimension
      `idim` and returns output tensors of dimension `odim`.
    input_dims: The input dimension(s) of the test `tf.keras.Model` instance.
    output_dim: The output dimension of the test `tf.keras.Model` instance.
    num_microbatches: The number of microbatches. None or an integer.
    is_eager: A `bool` that is `True` if the model should be run eagerly.
    x_input: `tf.Tensor` inputs to be tested.
    rng_seed: An `int` used to initialize model weights.
    registry: A `layer_registry.LayerRegistry` instance.

  Returns:
    A `tuple` `(computed_norm, true_norms)`. The first element contains the
    clipped gradient norms that are generated by
    `clip_grads.compute_gradient_norms()` under the setting given by the given
    model and layer generators. The second element contains the true clipped
    gradient norms under the aforementioned setting.
  """
  model = model_generator(layer_generator, input_dims, output_dim)
  model.compile(
      optimizer=tf.keras.optimizers.SGD(learning_rate=1.0),
      loss=tf.keras.losses.MeanSquaredError(
          reduction=tf.keras.losses.Reduction.SUM_OVER_BATCH_SIZE
      ),
      run_eagerly=is_eager,
  )
  y_pred = model(x_input)
  y_batch = tf.ones_like(y_pred)
  tf.keras.utils.set_random_seed(rng_seed)
  computed_norms = clip_grads.compute_gradient_norms(
      model,
      x_input,
      y_batch,
      layer_registry=registry,
      num_microbatches=num_microbatches,
  )
  tf.keras.utils.set_random_seed(rng_seed)
  true_norms = compute_true_gradient_norms(
      model, x_input, y_batch, num_microbatches
  )
  return (computed_norms, true_norms)


# ==============================================================================
# Model generators.
# ==============================================================================
def make_two_layer_sequential_model(layer_generator, input_dim, output_dim):
  """Creates a 2-layer sequential model."""
  model = tf.keras.Sequential()
  model.add(tf.keras.Input(shape=(input_dim,)))
  model.add(layer_generator(input_dim, output_dim))
  model.add(tf.keras.layers.Dense(1))
  return model


def make_three_layer_sequential_model(layer_generator, input_dim, output_dim):
  """Creates a 3-layer sequential model."""
  model = tf.keras.Sequential()
  model.add(tf.keras.Input(shape=(input_dim,)))
  layer1 = layer_generator(input_dim, output_dim)
  model.add(layer1)
  if isinstance(layer1, tf.keras.layers.Embedding):
    # Having multiple consecutive embedding layers does not make sense since
    # embedding layers only map integers to real-valued vectors.
    model.add(tf.keras.layers.Dense(output_dim))
  else:
    model.add(layer_generator(output_dim, output_dim))
  model.add(tf.keras.layers.Dense(1))
  return model


def make_two_layer_functional_model(layer_generator, input_dim, output_dim):
  """Creates a 2-layer 1-input functional model with a pre-output square op."""
  inputs = tf.keras.Input(shape=(input_dim,))
  layer1 = layer_generator(input_dim, output_dim)
  temp1 = layer1(inputs)
  temp2 = tf.square(temp1)
  outputs = tf.keras.layers.Dense(1)(temp2)
  return tf.keras.Model(inputs=inputs, outputs=outputs)


def make_two_tower_model(layer_generator, input_dim, output_dim):
  """Creates a 2-layer 2-input functional model."""
  inputs1 = tf.keras.Input(shape=(input_dim,))
  layer1 = layer_generator(input_dim, output_dim)
  temp1 = layer1(inputs1)
  inputs2 = tf.keras.Input(shape=(input_dim,))
  layer2 = layer_generator(input_dim, output_dim)
  temp2 = layer2(inputs2)
  temp3 = tf.add(temp1, temp2)
  outputs = tf.keras.layers.Dense(1)(temp3)
  return tf.keras.Model(inputs=[inputs1, inputs2], outputs=outputs)


def make_bow_model(layer_generator, input_dims, output_dim):
  del layer_generator
  inputs = tf.keras.Input(shape=input_dims)
  # For the Embedding layer, input_dim is the vocabulary size. This should
  # be distinguished from the input_dim argument, which is the number of ids
  # in eache example.
  emb_layer = tf.keras.layers.Embedding(input_dim=10, output_dim=output_dim)
  feature_embs = emb_layer(inputs)
  reduction_axes = tf.range(1, len(feature_embs.shape))
  example_embs = tf.expand_dims(
      tf.reduce_sum(feature_embs, axis=reduction_axes), axis=-1
  )
  return tf.keras.Model(inputs=inputs, outputs=example_embs)


def make_dense_bow_model(layer_generator, input_dims, output_dim):
  del layer_generator
  inputs = tf.keras.Input(shape=input_dims)
  # For the Embedding layer, input_dim is the vocabulary size. This should
  # be distinguished from the input_dim argument, which is the number of ids
  # in eache example.
  cardinality = 10
  emb_layer = tf.keras.layers.Embedding(
      input_dim=cardinality, output_dim=output_dim
  )
  feature_embs = emb_layer(inputs)
  reduction_axes = tf.range(1, len(feature_embs.shape))
  example_embs = tf.expand_dims(
      tf.reduce_sum(feature_embs, axis=reduction_axes), axis=-1
  )
  outputs = tf.keras.layers.Dense(1)(example_embs)
  return tf.keras.Model(inputs=inputs, outputs=outputs)


def make_weighted_bow_model(layer_generator, input_dims, output_dim):
  # NOTE: This model only accepts dense input tensors.
  del layer_generator
  inputs = tf.keras.Input(shape=input_dims)
  # For the Embedding layer, input_dim is the vocabulary size. This should
  # be distinguished from the input_dim argument, which is the number of ids
  # in eache example.
  cardinality = 10
  emb_layer = tf.keras.layers.Embedding(
      input_dim=cardinality, output_dim=output_dim
  )
  feature_embs = emb_layer(inputs)
  feature_weights = tf.random.uniform(tf.shape(feature_embs))
  weighted_embs = feature_embs * feature_weights
  reduction_axes = tf.range(1, len(weighted_embs.shape))
  example_embs = tf.expand_dims(
      tf.reduce_sum(weighted_embs, axis=reduction_axes), axis=-1
  )
  outputs = tf.keras.layers.Dense(1)(example_embs)
  return tf.keras.Model(inputs=inputs, outputs=outputs)


# ==============================================================================
# Factory functions.
# ==============================================================================
def get_nd_test_tensors(n: int):
  """Returns a list of candidate tests for a given dimension n."""
  return [
      tf.zeros((n,), dtype=tf.float64),
      tf.convert_to_tensor(range(n), dtype_hint=tf.float64),
  ]


def get_nd_test_batches(n: int):
  """Returns a list of candidate input batches of dimension n."""
  result = []
  tensors = get_nd_test_tensors(n)
  for batch_size in range(1, len(tensors) + 1, 1):
    combinations = list(
        itertools.combinations(get_nd_test_tensors(n), batch_size)
    )
    result = result + [tf.stack(ts, axis=0) for ts in combinations]
  return result


def get_dense_layer_generators():
  def sigmoid_dense_layer(b):
    return tf.keras.layers.Dense(b, activation='sigmoid')

  return {
      'pure_dense': lambda a, b: tf.keras.layers.Dense(b),
      'sigmoid_dense': lambda a, b: sigmoid_dense_layer(b),
  }


def get_dense_model_generators():
  return {
      'seq1': make_two_layer_sequential_model,
      'seq2': make_three_layer_sequential_model,
      'func1': make_two_layer_functional_model,
      'tower1': make_two_tower_model,
  }


def get_embedding_model_generators():
  return {
      'bow1': make_bow_model,
      'bow2': make_dense_bow_model,
      'weighted_bow1': make_weighted_bow_model,
  }


# ==============================================================================
# Main tests.
# ==============================================================================
class ClipGradsDirectTest(tf.test.TestCase, parameterized.TestCase):

  @parameterized.product(
      input_dim=[1, 2], clip_value=[1e-6, 0.5, 1.0, 2.0, 10.0, 1e6]
  )
  def test_clip_weights(self, input_dim, clip_value):
    tol = 1e-6
    for t in get_nd_test_tensors(input_dim):
      self.assertIsNone(clip_grads.compute_clip_weights(None, t))
      weights = clip_grads.compute_clip_weights(clip_value, t)
      self.assertAllLessEqual(t * weights, clip_value + tol)


class ClipGradsDenseLayerTest(tf.test.TestCase, parameterized.TestCase):

  @parameterized.product(
      model_name=list(get_dense_model_generators().keys()),
      layer_name=list(get_dense_layer_generators().keys()),
      input_dim=[4],
      output_dim=[1, 2],
      num_microbatches=[None, 1, 2],
      is_eager=[True, False],
  )
  def test_gradient_norms_on_various_models(
      self,
      model_name,
      layer_name,
      input_dim,
      output_dim,
      num_microbatches,
      is_eager,
  ):
    model_generator = get_dense_model_generators()[model_name]
    layer_generator = get_dense_layer_generators()[layer_name]
    x_batches = get_nd_test_batches(input_dim)
    default_registry = layer_registry.make_default_layer_registry()
    for x_batch in x_batches:
      if (
          num_microbatches is not None
          and x_batch.shape[0] % num_microbatches != 0
      ):
        continue
      if model_name == 'tower1':
        x_input = [x_batch, x_batch]
      else:
        x_input = x_batch
      (computed_norms, true_norms) = get_computed_and_true_norms(
          model_generator,
          layer_generator,
          input_dim,
          output_dim,
          num_microbatches,
          is_eager,
          x_input,
          registry=default_registry,
      )
      self.assertAllClose(computed_norms, true_norms, rtol=1e-3, atol=1e-2)


class ClipGradsEmbeddingLayerTest(tf.test.TestCase, parameterized.TestCase):

  # TODO(wkong): Test sparse input tensors when the GitHub CI environment
  # supports them for embeddings.
  @parameterized.product(
      x_batch=[
          # 2D inputs.
          tf.convert_to_tensor([[0, 1]], dtype_hint=tf.int32),
          tf.convert_to_tensor([[0, 1], [1, 1], [0, 0]], dtype_hint=tf.int32),
          tf.ragged.constant(
              [[0], [1], [], [0, 0], [0, 1], [1, 0], [1, 1]], dtype=tf.int32
          ),
          tf.ragged.constant(
              [[0], [1], [], [0, 0], [0, 1], [1, 0], [1, 1], [0, 1]],
              dtype=tf.int32,
          ),
          # 3D inputs.
          tf.convert_to_tensor([[[0, 1]]], dtype_hint=tf.int32),
          tf.convert_to_tensor(
              [[[0, 1]], [[1, 1]], [[0, 0]]], dtype_hint=tf.int32
          ),
          tf.ragged.constant(
              [[[0]], [[1]], [], [[0, 0]], [[0, 1]], [[1, 0]], [[1, 1]]],
              dtype=tf.int32,
          ),
          tf.ragged.constant(
              [[[0]], [[1]], [], [[0, 0]], [[0, 1]], [[1, 0]], [[1, 1]], [[0]]],
              dtype=tf.int32,
          ),
      ],
      model_name=list(get_embedding_model_generators().keys()),
      output_dim=[2],
      num_microbatches=[None, 1, 2],
      is_eager=[True],
  )
  def test_gradient_norms_on_various_models(
      self, x_batch, model_name, output_dim, num_microbatches, is_eager
  ):
    if (
        num_microbatches is not None
        and x_batch.shape[0] % num_microbatches != 0
    ):
      return
    valid_test_input = (
        not isinstance(x_batch, tf.RaggedTensor)
        and model_name == 'weighted_bow1'
    ) or (model_name != 'weighted_bow1')
    if valid_test_input:
      default_registry = layer_registry.make_default_layer_registry()
      model_generator = get_embedding_model_generators()[model_name]
      (computed_norms, true_norms) = get_computed_and_true_norms(
          model_generator=model_generator,
          layer_generator=None,
          input_dims=x_batch.shape[1:],
          output_dim=output_dim,
          num_microbatches=num_microbatches,
          is_eager=is_eager,
          x_input=x_batch,
          registry=default_registry,
      )
      self.assertAllClose(computed_norms, true_norms, rtol=1e-3, atol=1e-2)


class ClipGradsCustomLayerTest(tf.test.TestCase, parameterized.TestCase):

  @parameterized.product(
      input_dim=[1, 2],
      output_dim=[1, 2],
      num_microbatches=[None, 1, 2],
      is_eager=[True, False],
  )
  def test_gradient_norms_on_various_models(
      self, input_dim, output_dim, num_microbatches, is_eager
  ):
    registry = layer_registry.make_default_layer_registry()
    registry.insert(DoubleDense, double_dense_layer_computation)
    x_batches = get_nd_test_batches(input_dim)
    for x_batch in x_batches:
      if (
          num_microbatches is not None
          and x_batch.shape[0] % num_microbatches != 0
      ):
        continue
      (computed_norms, true_norms) = get_computed_and_true_norms(
          model_generator=make_two_layer_sequential_model,
          layer_generator=lambda a, b: DoubleDense(b),
          input_dims=input_dim,
          output_dim=output_dim,
          num_microbatches=num_microbatches,
          is_eager=is_eager,
          x_input=x_batch,
          registry=registry,
      )
      self.assertAllClose(computed_norms, true_norms, rtol=1e-3, atol=1e-2)


if __name__ == '__main__':
  tf.test.main()
