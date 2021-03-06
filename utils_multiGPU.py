import model
import tensorflow as tf
import re
import numpy as np

def tower_loss(scope, images, labels, is_training=True):
  """Calculate the total loss on a single tower running the CIFAR model.

  Args:
    scope: unique prefix string identifying the CIFAR tower, e.g. 'tower_0'
    images: Images. 4D tensor of shape [batch_size, height, width, 3].
    labels: Labels. 1D tensor of shape [batch_size].

  Returns:
     Tensor of shape [] containing the total loss for a batch of data
  """

  # Build the portion of the Graph calculating the losses. Note that we will
  # assemble the total_loss using a custom function below.
  resized_images = tf.image.resize_nearest_neighbor(images, (299, 299))

  acc = []
  loc = np.arange(100, 200, 10, dtype='int64')
  loc = [(i, j) for i in loc for j in loc]
  for i, loc_i in enumerate(loc):
    loc_x, loc_y = loc_i
    x_crop_i = resized_images[:, loc_x - 100:loc_x + 100, loc_y - 100:loc_y + 100, :]
    logits = model.inference(x_crop_i, is_training=is_training)
    _ = model.loss(logits, labels)
    acc_i = tf.reduce_mean(tf.cast(tf.equal(tf.arg_max(logits, 1), labels), tf.float32))
    acc += [acc_i]
    tf.get_variable_scope().reuse_variables()
  mean_acc = tf.reduce_mean(acc)

  # Assemble all of the losses for the current tower only.
  losses = tf.get_collection('losses', scope)
  cw_losses = tf.get_collection('cw_losses', scope)

  # Calculate the total loss for the current tower.
  total_loss = tf.add_n(losses, name='total_loss')

  # Attach a scalar summary to all individual losses and the total loss; do the
  # same for the averaged version of the losses.
  for l in losses + [total_loss]:
    # Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
    # session. This helps the clarity of presentation on tensorboard.
    loss_name = re.sub('%s_[0-9]*/' % model.TOWER_NAME, '', l.op.name)
    tf.summary.scalar(loss_name, l)

  return total_loss, cw_losses, mean_acc


def average_gradients(tower_grads):
  """Calculate the average gradient for each shared variable across all towers.

  Note that this function provides a synchronization point across all towers.

  Args:
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over individual gradients. The inner list is over the gradient
      calculation for each tower.
  Returns:
     List of pairs of (gradient, variable) where the gradient has been averaged
     across all towers.
  """
  average_grads = []
  for grad_and_vars in zip(*tower_grads):
    # Note that each grad_and_vars looks like the following:
    #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
    grads = []
    for g, _ in grad_and_vars:
      # Add 0 dimension to the gradients to represent the tower.
      expanded_g = tf.expand_dims(g, 0)

      # Append on a 'tower' dimension which we will average over below.
      grads.append(expanded_g)

    # Average over the 'tower' dimension.
    grad = tf.concat(axis=0, values=grads)
    grad = tf.reduce_mean(grad, 0)

    # Keep in mind that the Variables are redundant because they are shared
    # across towers. So .. we will just return the first tower's pointer to
    # the Variable.
    v = grad_and_vars[0][1]
    grad_and_var = (grad, v)
    average_grads.append(grad_and_var)
  return average_grads

