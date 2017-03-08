#! /usr/bin/env python
# Copyright 2017 Google Inc.
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

"""Main script to run training and evaluation of models.
"""

import tempfile
from pydoc import locate

import yaml
from six import string_types

from seq2seq import tasks
from seq2seq.configurable import _maybe_load_yaml
from seq2seq.data import input_pipeline
from seq2seq.training import utils as training_utils

import tensorflow as tf
from tensorflow.contrib.learn.python.learn import learn_runner
from tensorflow.contrib.learn.python.learn.estimators import run_config
from tensorflow.python.platform import gfile


tf.flags.DEFINE_string("task", "TextToTextInfer",
                       """The type of inference task to run. Must be defined
                       in seq2seq.tasks""")
tf.flags.DEFINE_string("task_params", "{}",
                       """Parameters to pass to the task class.
                       A YAML/JSON string.""")

tf.flags.DEFINE_string("config_path", None,
                       """Path to a YAML configuration file defining FLAG
                       values and hyperparameters. Refer to the documentation
                       for more details.""")

tf.flags.DEFINE_string("input_pipeline_train", None,
                       """Use this to overwrite the training input pipeline.
                       A YAML string.""")
tf.flags.DEFINE_string("input_pipeline_dev", None,
                       """Use this to overwrite the development input pipeline.
                       A YAML string.""")

tf.flags.DEFINE_string("buckets", None,
                       """Buckets input sequences according to these length.
                       A comma-separated list of sequence length buckets, e.g.
                       "10,20,30" would result in 4 buckets:
                       <10, 10-20, 20-30, >30. None disabled bucketing. """)
tf.flags.DEFINE_integer("batch_size", 16,
                        """Batch size used for training and evaluation.""")
tf.flags.DEFINE_string("output_dir", None,
                       """The directory to write model checkpoints and summaries
                       to. If None, a local temporary directory is created.""")

# Training parameters
tf.flags.DEFINE_string("schedule", None,
                       """Estimator function to call, defaults to
                       train_and_evaluate for local run""")
tf.flags.DEFINE_integer("train_steps", None,
                        """Maximum number of training steps to run.
                         If None, train forever.""")
tf.flags.DEFINE_integer("train_epochs", None,
                        """Maximum number of training epochs over the data.
                         If None, train forever.""")
tf.flags.DEFINE_integer("eval_every_n_steps", 1000,
                        "Run evaluation on validation data every N steps.")
tf.flags.DEFINE_integer("sample_every_n_steps", 500,
                        """Sample and print sequence predictions every N steps
                        during training.""")

# RunConfig Flags
tf.flags.DEFINE_integer("tf_random_seed", None,
                        """Random seed for TensorFlow initializers. Setting
                        this value allows consistency between reruns.""")
tf.flags.DEFINE_integer("save_checkpoints_secs", None,
                        """Save checkpoints every this many seconds.
                        Can not be specified with save_checkpoints_steps.""")
tf.flags.DEFINE_integer("save_checkpoints_steps", None,
                        """Save checkpoints every this many steps.
                        Can not be specified with save_checkpoints_secs.""")
tf.flags.DEFINE_integer("keep_checkpoint_max", 5,
                        """Maximum number of recent checkpoint files to keep.
                        As new files are created, older files are deleted.
                        If None or 0, all checkpoint files are kept.""")
tf.flags.DEFINE_integer("keep_checkpoint_every_n_hours", 4,
                        """In addition to keeping the most recent checkpoint
                        files, keep one checkpoint file for every N hours of
                        training.""")

FLAGS = tf.flags.FLAGS

def create_experiment(output_dir):
  """
  Creates a new Experiment instance.

  Args:
    output_dir: Output directory for model checkpoints and summaries.
  """

  config = run_config.RunConfig(
      tf_random_seed=FLAGS.tf_random_seed,
      save_checkpoints_secs=FLAGS.save_checkpoints_secs,
      save_checkpoints_steps=FLAGS.save_checkpoints_steps,
      keep_checkpoint_max=FLAGS.keep_checkpoint_max,
      keep_checkpoint_every_n_hours=FLAGS.keep_checkpoint_every_n_hours
  )

  # Load the correct task class
  task_class = locate(FLAGS.task) or getattr(tasks, FLAGS.task)
  task = task_class(
      params=_maybe_load_yaml(FLAGS.task_params))

  # One the main worker, save training options
  if config.is_chief:
    gfile.MakeDirs(output_dir)
    train_options = training_utils.TrainOptions(
        task=FLAGS.task,
        task_params=task.params)
    train_options.dump(output_dir)

  bucket_boundaries = None
  if FLAGS.buckets:
    bucket_boundaries = list(map(int, FLAGS.buckets.split(",")))

  # Training data input pipeline
  train_input_pipeline = input_pipeline.make_input_pipeline_from_def(
      def_dict=_maybe_load_yaml(FLAGS.input_pipeline_train),
      mode=tf.contrib.learn.ModeKeys.TRAIN)

  # Development data input pipeline
  dev_input_pipeline = input_pipeline.make_input_pipeline_from_def(
      def_dict=_maybe_load_yaml(FLAGS.input_pipeline_dev),
      mode=tf.contrib.learn.ModeKeys.EVAL,
      shuffle=False, num_epochs=1)

  # Create training input function
  train_input_fn = training_utils.create_input_fn(
      pipeline=train_input_pipeline,
      batch_size=FLAGS.batch_size,
      bucket_boundaries=bucket_boundaries)

  # Create eval input function
  eval_input_fn = training_utils.create_input_fn(
      pipeline=dev_input_pipeline,
      batch_size=FLAGS.batch_size,
      allow_smaller_final_batch=True)

  def model_fn(features, labels, params, mode):
    """Builds the model graph"""
    model = task.create_model(mode=mode)
    return model(features, labels, params)

  estimator = tf.contrib.learn.Estimator(
      model_fn=model_fn,
      model_dir=output_dir,
      config=config,
      params=task.params["model_params"])

  train_hooks = task.create_training_hooks(estimator)
  eval_metrics = task.create_metrics()

  experiment = tf.contrib.learn.Experiment(
      estimator=estimator,
      train_input_fn=train_input_fn,
      eval_input_fn=eval_input_fn,
      min_eval_frequency=FLAGS.eval_every_n_steps,
      train_steps=FLAGS.train_steps,
      eval_steps=None,
      eval_metrics=eval_metrics,
      train_monitors=train_hooks)

  return experiment


def main(_argv):
  """The entrypoint for the script"""

  # Load flags from config file
  if FLAGS.config_path:
    with gfile.GFile(FLAGS.config_path) as config_file:
      config_flags = yaml.load(config_file)
      for flag_key, flag_value in config_flags.items():
        setattr(FLAGS, flag_key, flag_value)

  if FLAGS.save_checkpoints_secs is None \
    and FLAGS.save_checkpoints_steps is None:
    FLAGS.save_checkpoints_secs = 600
    tf.logging.info("Setting save_checkpoints_secs to %d",
                    FLAGS.save_checkpoints_secs)

  if not FLAGS.output_dir:
    FLAGS.output_dir = tempfile.mkdtemp()

  learn_runner.run(
      experiment_fn=create_experiment,
      output_dir=FLAGS.output_dir,
      schedule=FLAGS.schedule)


if __name__ == "__main__":
  tf.logging.set_verbosity(tf.logging.INFO)
  tf.app.run()
