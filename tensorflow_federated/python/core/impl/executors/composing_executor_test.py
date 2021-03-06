# Lint as: python3
# Copyright 2019, The TensorFlow Federated Authors.
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

import asyncio
import collections

from absl.testing import absltest
import tensorflow as tf

from tensorflow_federated.python.common_libs import anonymous_tuple
from tensorflow_federated.python.core.api import computation_types
from tensorflow_federated.python.core.api import computations
from tensorflow_federated.python.core.api import intrinsics
from tensorflow_federated.python.core.impl.compiler import placement_literals
from tensorflow_federated.python.core.impl.compiler import type_factory
from tensorflow_federated.python.core.impl.executors import caching_executor
from tensorflow_federated.python.core.impl.executors import composing_executor
from tensorflow_federated.python.core.impl.executors import eager_tf_executor
from tensorflow_federated.python.core.impl.executors import federating_executor
from tensorflow_federated.python.core.impl.executors import reference_resolving_executor
from tensorflow_federated.python.core.impl.executors import thread_delegating_executor

tf.compat.v1.enable_v2_behavior()


def _create_bottom_stack():
  return reference_resolving_executor.ReferenceResolvingExecutor(
      caching_executor.CachingExecutor(
          thread_delegating_executor.ThreadDelegatingExecutor(
              eager_tf_executor.EagerTFExecutor())))


def _create_worker_stack():
  return federating_executor.FederatingExecutor({
      placement_literals.SERVER: _create_bottom_stack(),
      placement_literals.CLIENTS: [_create_bottom_stack() for _ in range(2)],
      None: _create_bottom_stack()
  })


def _create_middle_stack(children):
  return reference_resolving_executor.ReferenceResolvingExecutor(
      caching_executor.CachingExecutor(
          composing_executor.ComposingExecutor(_create_bottom_stack(),
                                               children)))


def _create_test_executor():
  executor = _create_middle_stack([
      _create_middle_stack([_create_worker_stack() for _ in range(3)]),
      _create_middle_stack([_create_worker_stack() for _ in range(3)]),
  ])
  # 2 clients per worker stack * 3 worker stacks * 2 middle stacks
  num_clients = 12
  return executor, num_clients


def _invoke(ex, comp, arg=None):
  loop = asyncio.get_event_loop()
  v1 = loop.run_until_complete(ex.create_value(comp))
  if arg is not None:
    type_spec = v1.type_signature.parameter
    v2 = loop.run_until_complete(ex.create_value(arg, type_spec))
  else:
    v2 = None
  v3 = loop.run_until_complete(ex.create_call(v1, v2))
  return loop.run_until_complete(v3.compute())


class ComposingExecutorTest(absltest.TestCase):

  def test_federated_value_at_server(self):

    @computations.federated_computation
    def comp():
      return intrinsics.federated_value(10, placement_literals.SERVER)

    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, 10)

  def test_federated_value_at_clients(self):

    @computations.federated_computation
    def comp():
      return intrinsics.federated_value(10, placement_literals.CLIENTS)

    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, 10)

  def test_federated_eval_at_server(self):

    @computations.federated_computation
    def comp():
      return_five = computations.tf_computation(lambda: 5)
      return intrinsics.federated_eval(return_five, placement_literals.SERVER)

    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, 5)

  def test_federated_eval_at_clients(self):

    @computations.federated_computation
    def comp():
      return_five = computations.tf_computation(lambda: 5)
      return intrinsics.federated_eval(return_five, placement_literals.CLIENTS)

    executor, num_clients = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertIsInstance(result, list)
    self.assertLen(result, num_clients)
    for x in result:
      self.assertEqual(x, 5)

  def test_federated_map(self):

    @computations.tf_computation(tf.int32)
    def add_one(x):
      return x + 1

    @computations.federated_computation
    def comp():
      value = intrinsics.federated_value(10, placement_literals.CLIENTS)
      return intrinsics.federated_map(add_one, value)

    executor, num_clients = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, [10 + 1] * num_clients)

  def test_federated_aggregate(self):

    @computations.tf_computation(tf.int32, tf.int32)
    def add_int(x, y):
      return x + y

    @computations.tf_computation(tf.int32)
    def add_five(x):
      return x + 5

    @computations.federated_computation
    def comp():
      value = intrinsics.federated_value(10, placement_literals.CLIENTS)
      return intrinsics.federated_aggregate(value, 0, add_int, add_int,
                                            add_five)

    executor, num_clients = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, 10 * num_clients + 5)

  def test_federated_aggregate_of_nested_tuple(self):
    test_type = computation_types.NamedTupleType([
        ('a', (tf.int32, tf.float32)),
    ])

    @computations.tf_computation(test_type, test_type)
    def add_test_type(x, y):
      return collections.OrderedDict([
          ('a', (x.a[0] + y.a[0], x.a[1] + y.a[1])),
      ])

    @computations.tf_computation(test_type)
    def add_five_and_three(x):
      return collections.OrderedDict([('a', (x.a[0] + 5, x.a[1] + 3.0))])

    @computations.federated_computation
    def comp():
      value = intrinsics.federated_value(
          collections.OrderedDict([('a', (10, 2.0))]),
          placement_literals.CLIENTS)
      zero = collections.OrderedDict([('a', (0, 0.0))])
      return intrinsics.federated_aggregate(value, zero, add_test_type,
                                            add_test_type, add_five_and_three)

    executor, num_clients = _create_test_executor()
    result = _invoke(executor, comp)
    excepted_result = anonymous_tuple.AnonymousTuple([
        ('a',
         anonymous_tuple.AnonymousTuple([
             (None, 10 * num_clients + 5),
             (None, 2.0 * num_clients + 3.0),
         ])),
    ])
    self.assertEqual(result, excepted_result)

  def test_federated_broadcast(self):

    @computations.tf_computation(tf.int32)
    def add_one(x):
      return x + 1

    @computations.federated_computation
    def comp():
      value_at_server = intrinsics.federated_value(10,
                                                   placement_literals.SERVER)
      value_at_clients = intrinsics.federated_broadcast(value_at_server)
      return intrinsics.federated_map(add_one, value_at_clients)

    executor, num_clients = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, [10 + 1] * num_clients)

  def test_federated_map_at_server(self):

    @computations.tf_computation(tf.int32)
    def add_one(x):
      return x + 1

    @computations.federated_computation
    def comp():
      value = intrinsics.federated_value(10, placement_literals.SERVER)
      return intrinsics.federated_map(add_one, value)

    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, 10 + 1)

  def test_federated_zip_at_server_unnamed(self):

    @computations.federated_computation
    def comp():
      return intrinsics.federated_zip([
          intrinsics.federated_value(10, placement_literals.SERVER),
          intrinsics.federated_value(20, placement_literals.SERVER),
      ])

    self.assertEqual(comp.type_signature.compact_representation(),
                     '( -> <int32,int32>@SERVER)')
    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    excepted_result = anonymous_tuple.AnonymousTuple([(None, 10), (None, 20)])
    self.assertEqual(result, excepted_result)

  def test_federated_zip_at_server_named(self):

    @computations.federated_computation
    def comp():
      return intrinsics.federated_zip(
          collections.OrderedDict([
              ('A', intrinsics.federated_value(10, placement_literals.SERVER)),
              ('B', intrinsics.federated_value(20, placement_literals.SERVER)),
          ]))

    self.assertEqual(comp.type_signature.compact_representation(),
                     '( -> <A=int32,B=int32>@SERVER)')
    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    excepted_result = anonymous_tuple.AnonymousTuple([('A', 10), ('B', 20)])
    self.assertEqual(result, excepted_result)

  def test_federated_zip_at_clients_unnamed(self):

    @computations.federated_computation
    def comp():
      return intrinsics.federated_zip([
          intrinsics.federated_value(10, placement_literals.CLIENTS),
          intrinsics.federated_value(20, placement_literals.CLIENTS),
      ])

    self.assertEqual(comp.type_signature.compact_representation(),
                     '( -> {<int32,int32>}@CLIENTS)')
    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    for value in result:
      excepted_value = anonymous_tuple.AnonymousTuple([(None, 10), (None, 20)])
      self.assertEqual(value, excepted_value)

  def test_federated_zip_at_clients_named(self):

    @computations.federated_computation
    def comp():
      return intrinsics.federated_zip(
          collections.OrderedDict([
              ('A', intrinsics.federated_value(10, placement_literals.CLIENTS)),
              ('B', intrinsics.federated_value(20, placement_literals.CLIENTS)),
          ]))

    self.assertEqual(comp.type_signature.compact_representation(),
                     '( -> {<A=int32,B=int32>}@CLIENTS)')
    executor, _ = _create_test_executor()
    result = _invoke(executor, comp)
    for value in result:
      excepted_value = anonymous_tuple.AnonymousTuple([('A', 10), ('B', 20)])
      self.assertEqual(value, excepted_value)

  def test_federated_sum(self):

    @computations.federated_computation
    def comp():
      value = intrinsics.federated_value(10, placement_literals.CLIENTS)
      return intrinsics.federated_sum(value)

    executor, num_clients = _create_test_executor()
    result = _invoke(executor, comp)
    self.assertEqual(result, 10 * num_clients)

  def test_federated_mean(self):

    @computations.federated_computation(type_factory.at_clients(tf.float32))
    def comp(x):
      return intrinsics.federated_mean(x)

    executor, num_clients = _create_test_executor()
    arg = [float(x + 1) for x in range(num_clients)]
    result = _invoke(executor, comp, arg)
    self.assertEqual(result, 6.5)

  def test_federated_weighted_mean(self):

    @computations.federated_computation(
        type_factory.at_clients(tf.float32),
        type_factory.at_clients(tf.float32))
    def comp(x, y):
      return intrinsics.federated_mean(x, y)

    executor, num_clients = _create_test_executor()
    arg = ([float(x + 1) for x in range(num_clients)], [1.0, 2.0, 3.0] * 4)
    result = _invoke(executor, comp, arg)
    self.assertAlmostEqual(result, 6.83333333333, places=3)


if __name__ == '__main__':
  absltest.main()
