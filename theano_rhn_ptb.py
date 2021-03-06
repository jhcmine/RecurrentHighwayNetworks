"""Word level next step prediction for the PTB dataset using Recurrent Highway Networks - Theano implementation.

To run:
$ python theano_rhn_ptb.py

References:
[1] Zilly, J, Srivastava, R, Koutnik, J, Schmidhuber, J., "Recurrent Highway Networks", 2016
[2] Gal, Y, "A Theoretically Grounded Application of Dropout in Recurrent Neural Networks", 2015.
[3] Zaremba, W, Sutskever, I, Vinyals, O, "Recurrent neural network regularization", 2014.
[4] Press, O, Wolf, L, "Using the Output Embedding to Improve Language Models", 2016.
"""
from __future__ import absolute_import, division, print_function

from copy import deepcopy
import time
import sys
import logging

import numpy as np

from sacred import Experiment
from theano_rhn import Model
from data.reader import data_iterator


ex_name = 'theano_rhn_ptb'
ex = Experiment(ex_name)

# Use a logger which outputs both to console and to a file.
logger = logging.getLogger('custom_logger')
logger.setLevel(logging.INFO)
fh = logging.FileHandler(ex_name + '.log')
ch = logging.StreamHandler(sys.stdout)
fh.setLevel(logging.INFO)
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)
ex.logger = logger


@ex.config
def hyperparameters():
  data_path = 'data'
  init_scale = 0.04            # uniform weight initialization values are sampled from U[-init_scale, init_scale]
  init_T_bias = -2.0           # init scheme for the bias of the T non-linearity
  init_other_bias = 'uniform'  # init scheme for all other biases (rhn_train.py uses uniform init)
  num_layers = 1               # number of stacked RHN layers
  depth = 10                   # the recurrence depth within each RHN layer, i.e. number of micro-timesteps per timestep
  learning_rate = 0.2
  lr_decay = 1.02
  weight_decay = 1e-7
  max_grad_norm = 10
  num_steps = 35
  hidden_size = 830
  max_epoch = 20
  max_max_epoch = 300
  batch_size = 20
  drop_x = 0.25                # variational dropout rate over input word embeddings
  drop_i = 0.75                # variational dropout rate over inputs of RHN layers(s), applied seperately in each RHN layer
  drop_s = 0.25                # variational dropout rate over recurrent state
  drop_o = 0.75                # variational dropout rate over outputs of RHN layer(s), applied before classification layer
  tied_embeddings = True       # whether to use same embedding matrix for both input and output word embeddings
  tied_noise = True            # whether to use same dropout masks for the T and H non-linearites (tied in rhn_train.py)
  load_model = ''
  vocab_size = 10000

class Config:
  pass
C = Config()

@ex.capture
def get_config(_config):
  C.__dict__ = dict(_config)
  return C


def get_data(data_path):
  from data import reader
  raw_data = reader.ptb_raw_data(data_path)
  return reader, raw_data

def get_noise_x(x, config, drop_x):
  """Get a random (variational) dropout noise matrix for input words.
  Return value is generated by the CPU (rather than directly on the GPU, as is done for other noise matrices).
  """
  keep_x = 1.0 - drop_x
  if keep_x < 1.0:
    noise_x = (np.random.random_sample((config.batch_size, config.num_steps)) < keep_x).astype(np.float32) / keep_x
    for b in range(config.batch_size):
      for n1 in range(config.num_steps):
        for n2 in range(n1 + 1, config.num_steps):
          if x[b][n2] == x[b][n1]:
            noise_x[b][n2] = noise_x[b][n1]
            break
  else:
    noise_x = np.ones((config.batch_size, config.num_steps), dtype=np.float32)
  return noise_x


def run_epoch(m, data, config, is_train, verbose=False, log=None):
  """Run the model on the given data."""
  epoch_size = ((len(data) // config.batch_size) - 1) // config.num_steps
  start_time = time.time()
  costs = 0.0
  iters = 0
  m.reset_hidden_state()
  for step, (x, y) in enumerate(data_iterator(data, config.batch_size, config.num_steps)):
    if is_train:
      noise_x = get_noise_x(x, config, config.drop_x)
      cost = m.train(x, y, noise_x)
    else:
      cost = m.evaluate(x, y)
    costs += cost
    iters += config.num_steps
    if verbose and step % (epoch_size // 10) == 10:
      log.info("%.3f perplexity: %.3f speed: %.0f wps" % (step * 1.0 / epoch_size, np.exp(costs / iters),
                                                       iters * config.batch_size / (time.time() - start_time)))
  return np.exp(costs / iters)


@ex.automain
def main(_run, _log):

  log = _log
  from sacred.commands import _format_config  # brittle: get a string of what ex.commands['print_config']() prints.
  config_str = _format_config(_run.config, _run.config_modifications)

  log.info(config_str)
  config = get_config()
  reader, (train_data, valid_data, test_data, _) = get_data(config.data_path)

  log.info('Compiling (batched) model...')
  m = Model(config)
  log.info('Done. Number of parameters: %d' % m.num_params)

  trains, vals, tests, best_val = [np.inf], [np.inf], [np.inf], np.inf

  for i in range(config.max_max_epoch):
    lr_decay = config.lr_decay ** max(i - config.max_epoch + 1, 0.0)
    m.assign_lr(config.learning_rate / lr_decay)

    log.info("Epoch: %d Learning rate: %.3f" % (i + 1, m.lr))

    train_perplexity = run_epoch(m, train_data, config, is_train=True, verbose=True, log=log)
    log.info("Epoch: %d Train Perplexity: %.3f, Bits: %.3f" % (i + 1, train_perplexity, np.log2(train_perplexity)))

    valid_perplexity = run_epoch(m, valid_data, config, is_train=False)
    log.info("Epoch: %d Valid Perplexity (batched): %.3f, Bits: %.3f" % (i + 1, valid_perplexity, np.log2(valid_perplexity)))

    test_perplexity = run_epoch(m, test_data, config, is_train=False)
    log.info("Epoch: %d Test Perplexity (batched): %.3f, Bits: %.3f" % (i + 1, test_perplexity, np.log2(test_perplexity)))

    trains.append(train_perplexity)
    vals.append(valid_perplexity)
    tests.append(test_perplexity)

    if valid_perplexity < best_val:
      best_val = valid_perplexity
      log.info("Best Batched Valid Perplexity improved to %.03f" % best_val)
      save_path = './' + ex_name + '_' + str(config.seed) + '_best_model.pkl'
      m.save(save_path)
      log.info("Saved to: %s" % save_path)

  log.info("Training is over.")
  best_val_epoch = np.argmin(vals)
  log.info("Best Batched Validation Perplexity %.03f (Bits: %.3f) was at Epoch %d" %
        (vals[best_val_epoch], np.log2(vals[best_val_epoch]), best_val_epoch))
  log.info("Training Perplexity at this Epoch was %.03f, Bits: %.3f" %
        (trains[best_val_epoch], np.log2(trains[best_val_epoch])))
  log.info("Batched Test Perplexity at this Epoch was %.03f, Bits: %.3f" %
        (tests[best_val_epoch], np.log2(tests[best_val_epoch])))

  non_batched_config = deepcopy(config)
  non_batched_config.batch_size = 1
  non_batched_config.load_model = save_path

  log.info('Compiling (non-batched) model...')
  m_non_batched = Model(non_batched_config)
  log.info('Done. Number of parameters: %d' % m_non_batched.num_params)

  log.info("Testing on non-batched Valid ...")
  valid_perplexity = run_epoch(m_non_batched, valid_data, non_batched_config, is_train=False, verbose=True, log=log)
  log.info("Full Valid Perplexity: %.3f, Bits: %.3f" % (valid_perplexity, np.log2(valid_perplexity)))

  log.info("Testing on non-batched Test ...")
  test_perplexity = run_epoch(m_non_batched, test_data, non_batched_config, is_train=False, verbose=True, log=log)
  log.info("Full Test Perplexity: %.3f, Bits: %.3f" % (test_perplexity, np.log2(test_perplexity)))

  return vals[best_val_epoch]

