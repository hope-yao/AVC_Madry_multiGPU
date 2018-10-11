# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

"""A binary to train CIFAR-10 using multiple GPUs with synchronous updates.

Accuracy:
train_multiGPU.py achieves ~86% accuracy after 100K steps (256
epochs of data) as judged by cifar10_eval.py.

Speed: With batch_size 128.

System        | Step Time (sec/batch)  |     Accuracy
--------------------------------------------------------------------
1 Tesla K20m  | 0.35-0.60              | ~86% at 60K steps  (5 hours)
1 Tesla K40m  | 0.25-0.35              | ~86% at 100K steps (4 hours)
2 Tesla K20m  | 0.13-0.20              | ~84% at 30K steps  (2.5 hours)
3 Tesla K20m  | 0.13-0.18              | ~84% at 30K steps
4 Tesla K20m  | ~0.10                  | ~84% at 30K steps

Usage:
Please see the tutorial and website for how to download the CIFAR-10
data set, compile the program and train the model.

http://tensorflow.org/tutorials/deep_cnn/
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os.path
import re
import time

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from utils_multiGPU import *
import model
from pgd_attack import *


def log_output(hist, sess, accuracy, loss, feed_dict, feed_dict_adv, feed_dict_test, feed_dict_test_adv):
    ## training
    # natural
    train_acc_i, train_loss_i = sess.run([accuracy, loss], feed_dict)
    hist['train_acc'] += [train_acc_i]
    hist['train_loss'] += [train_loss_i]
    # adversarial
    train_adv_acc_i, train_adv_loss_i = sess.run([accuracy, loss], feed_dict_adv)
    hist['train_adv_acc'] += [train_adv_acc_i]
    hist['train_adv_loss'] += [train_adv_loss_i]

    ## test
    test_acc_i, test_loss_i = sess.run([accuracy, loss], feed_dict_test)
    hist['test_acc'] += [test_acc_i]
    hist['test_loss'] += [test_loss_i]
    # adversarial
    test_adv_acc_i, test_adv_loss_i = sess.run([accuracy, loss], feed_dict_test_adv)
    hist['test_adv_acc'] += [test_adv_acc_i]
    hist['test_adv_loss'] += [test_adv_loss_i]

    print('train_acc:{:.4f}       train_loss:{:.4f}'.format(train_acc_i, train_loss_i))
    print('train_adv_acc:{:.4f}      train_adv_loss:{:.4f}'.format(train_adv_acc_i, train_adv_loss_i))
    print('test_acc:{:.4f}       test_loss:{:.4f}'.format(test_acc_i, test_loss_i))
    print('test_adv_acc:{:.4f}      test_adv_loss:{:.4f}'.format(test_adv_acc_i, test_adv_loss_i))

    return hist


def load_data():
    ## load training data ##
    from random import shuffle
    train_data = np.load('./data/train_data.npy', encoding=('latin1')).item()
    train_images = train_data['image']
    train_labels = train_data['label']
    idx = list(range(len(train_images)))
    shuffle(idx)
    train_images = train_images[idx]
    train_labels = train_labels[idx]
    # testing
    test_data = np.load('./data/val_data.npy', encoding=('latin1')).item()
    test_images = test_data['image']
    test_labels = test_data['label']
    idx_v = list(range(len(test_images)))
    shuffle(idx_v)
    test_images = test_images[idx_v]
    test_labels = test_labels[idx_v]
    return train_images, train_labels, test_images, test_labels

def train():
    """Train CIFAR-10 for a number of steps."""
    with tf.Graph().as_default(), tf.device('/cpu:0'):
        # Create a variable to count the number of train() calls. This equals the
        # number of batches processed * FLAGS.num_gpus.
        global_step = tf.get_variable(
            'global_step', [],
            initializer=tf.constant_initializer(0), trainable=False)

        image_batch_pl = tf.placeholder(tf.float32,  shape = (batch_size, 64, 64, 3), name = 'input_images')
        label_batch_pl = tf.placeholder(tf.int64, shape=(batch_size), name='labels')
        is_training_pl = tf.placeholder(tf.bool, shape=(), name='labels')
        lr = 1e-4
        opt = tf.train.AdamOptimizer(lr)


        # BUILD MODEL
        tower_grads = []
        adv_grads = []
        accuracy =[]
        batch_size_i = batch_size // FLAGS.num_gpus
        with tf.variable_scope(tf.get_variable_scope()):
            for i in xrange(FLAGS.num_gpus):
                with tf.device('/gpu:%d' % i):
                    with tf.name_scope('%s_%d' % (model.TOWER_NAME, i)) as scope:
                        # Calculate the loss for one tower of the CIFAR model. This function
                        # constructs the entire CIFAR model but shares the variables across
                        # all towers.
                        image_batch_pl_i = image_batch_pl[i*batch_size_i:(i+1)*batch_size_i]
                        label_batch_pl_i = label_batch_pl[i*batch_size_i:(i+1)*batch_size_i]
                        loss, acc_i = tower_loss(scope, image_batch_pl_i, label_batch_pl_i, is_training_pl)
                        adv_grad_i = tf.gradients(loss, image_batch_pl_i)[0]


                        batchnorm_updates = tf.get_collection(tf.GraphKeys.UPDATE_OPS, scope=scope)

                        # Reuse variables for the next tower.
                        tf.get_variable_scope().reuse_variables()

                        # Retain the summaries from the final tower.
                        summaries = tf.get_collection(tf.GraphKeys.SUMMARIES, scope)

                        # Calculate the gradients for the batch of data on this CIFAR tower.
                        grads = opt.compute_gradients(loss)

                        # Keep track of the gradients across all towers.
                        tower_grads.append(grads)
                        # track all adversarial gradients, by Hope
                        adv_grads.append(adv_grad_i)
                        accuracy.append(acc_i)

        batchnorm_updates_op = tf.group(*batchnorm_updates)
        adv_grads = tf.concat(adv_grads, 0)
        accuracy = tf.reduce_mean(accuracy)
        # We must calculate the mean of each gradient. Note that this is the
        # synchronization point across all towers.
        grads = average_gradients(tower_grads)

        ## SUMMARY
        # Add a summary to track the learning rate.
        summaries.append(tf.summary.scalar('learning_rate', lr))
        # Add histograms for gradients.
        for grad, var in grads:
            if grad is not None:
                summaries.append(tf.summary.histogram(var.op.name + '/gradients', grad))
        # Apply the gradients to adjust the shared variables.
        apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)
        # Add histograms for trainable variables.
        for var in tf.trainable_variables():
            summaries.append(tf.summary.histogram(var.op.name, var))
        # Build the summary operation from the last tower summaries.
        summary_op = tf.summary.merge(summaries)
        # Track the moving averages of all trainable variables.
        variable_averages = tf.train.ExponentialMovingAverage(model.MOVING_AVERAGE_DECAY, global_step)
        variables_averages_op = variable_averages.apply(tf.trainable_variables())
         #Group all updates to into a single train op.
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            # train_op = tf.group(apply_gradient_op, batchnorm_updates_op, variables_averages_op)
            train_op = tf.group(apply_gradient_op, variables_averages_op)

        ## RESTORE
        # Create a saver. by Guangyu
        g_list = [op.name for op in tf.get_default_graph().get_operations() if op.op_def and op.op_def.name=='VariableV2']
        not_restore = [str(g)+':0' for g in g_list if 'ExponentialMovingAverage' in g]
        not_resotre = not_restore.append('global_step:0')
        restore_list = [v for v in tf.global_variables() if v.name not in not_restore]
        saver = tf.train.Saver(var_list = restore_list)
        # Build an initialization operation to run below.
        init = tf.global_variables_initializer()

        ## INIT
        sess = tf.Session(config=tf.ConfigProto(
            allow_soft_placement=True,
            log_device_placement=FLAGS.log_device_placement))
        # Start running operations on the Graph. allow_soft_placement must be set to
        # True to build towers on GPU, as some of the ops do not have GPU
        # implementations.
        sess.run(init)
        summary_writer = tf.summary.FileWriter(FLAGS.train_dir, sess.graph)
        # saver.restore(sess, "/home/hope-yao/Documents/models/tutorials/image/AVC_Madry_multiGPU_pretrain/model_save_base_final/center_loss.ckpt")

        ## LOAD DATA
        train_images, train_labels, test_images, test_labels = load_data()
        itr_per_epoch = train_images.shape[0] // batch_size
        itr_per_epoch_test = test_images.shape[0] // batch_size
        hist = {'train_loss': [],
                'train_acc': [],
                'train_adv_loss': [],
                'train_adv_acc': [],
                'test_loss': [],
                'test_acc': [],
                'test_adv_loss': [],
                'test_adv_acc': []}

        ## START TRAINING
        for ep_i in xrange(FLAGS.max_epoch):
            for itr_i in range(itr_per_epoch):
                start_time = time.time()

                x_batch_nat = train_images[itr_i * batch_size:(1 + itr_i) * batch_size]
                y_batch = train_labels[itr_i * batch_size:(1 + itr_i) * batch_size]
                feed_dict = {image_batch_pl: x_batch_nat,
                             label_batch_pl: y_batch,
                             is_training_pl: False}
                x_batch_adv = get_PGD(sess, adv_grads, feed_dict, image_batch_pl)
                feed_dict_adv = {image_batch_pl: x_batch_adv,
                                 label_batch_pl: y_batch,
                                 is_training_pl: True}
                _, loss_value = sess.run([train_op, loss], feed_dict=feed_dict_adv)
                # loss_value = sess.run(loss, feed_dict=feed_dict_adv)

                if itr_i%10==0:
                    # output training
                    duration = time.time() - start_time
                    num_examples_per_step = batch_size * 10
                    examples_per_sec = num_examples_per_step / duration
                    print('%s: ep %d, itr %d, loss = %.2f,  %.1f examples/sec' %(datetime.now(), ep_i, itr_i, loss_value, examples_per_sec))

                # output testing
                if itr_i%100==0:
                    x_batch_adv = get_PGD(sess, adv_grads, feed_dict, image_batch_pl)
                    feed_dict_adv = {image_batch_pl: x_batch_adv,
                                     label_batch_pl: y_batch,
                                     is_training_pl: False}

                    testing_batch_i = np.random.choice(itr_per_epoch_test, 1)[0]  # randomly pick a batch for testing
                    x_batch_nat_test = test_images[testing_batch_i * batch_size:(1 + testing_batch_i) * batch_size]
                    y_batch_test = test_labels[testing_batch_i * batch_size:(1 + testing_batch_i) * batch_size]
                    feed_dict_test = {image_batch_pl: x_batch_nat_test,
                                      label_batch_pl: y_batch_test,
                                      is_training_pl: False}

                    x_batch_adv_test = get_PGD(sess, adv_grads, feed_dict_test, image_batch_pl)
                    feed_dict_test_adv = {image_batch_pl: x_batch_adv_test,
                                          label_batch_pl: y_batch_test,
                                          is_training_pl: False}

                    hist = log_output(hist, sess, accuracy, loss, feed_dict, feed_dict_adv, feed_dict_test, feed_dict_test_adv)
                    np.save(os.path.join(log_dir, 'hist'), hist)

            if ep_i%10==0:
                saver.save(sess, os.path.join(log_dir, 'AVC_Madry_multiGPU_ep{}.ckpt'.format(ep_i)))


# if step % 100 == 0:
#  summary_str = sess.run(summary_op)
#  summary_writer.add_summary(summary_str, step)
#
# # Save the model checkpoint periodically.
# if step % 1000 == 0 or (step + 1) == FLAGS.max_steps:
#   checkpoint_path = os.path.join(FLAGS.train_dir, 'model.ckpt')
#   saver.save(sess, checkpoint_path, global_step=step)


if __name__ == '__main__':

    FLAGS = tf.app.flags.FLAGS

    tf.app.flags.DEFINE_string('train_dir', '/tmp/cifar10_train',
                               """Directory where to write event logs """
                               """and checkpoint.""")
    tf.app.flags.DEFINE_integer('max_epoch', 2000,
                                """Number of batches to run.""")
    tf.app.flags.DEFINE_integer('num_gpus', 2,
                                """How many GPUs to use.""")
    tf.app.flags.DEFINE_boolean('log_device_placement', False,
                                """Whether to log device placement.""")
    batch_size = 24  # split on 4 or 8 GPU, each GPU has 32 or 16

    log_dir = './model_save_base_madry'
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)

    train()