# Copyright 2023 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# pylint: disable=no-name-in-module
"""
Injection mindspore.nn for MindNLP
"""
import operator
from typing import OrderedDict, List
from functools import reduce, partial
import math
import types
import mindspore.experimental
import mindspore.experimental.optim
from packaging import version
import numpy as np
import mindspore
import mindspore.common.dtype as mstype
from mindspore._c_expression import Tensor as Tensor_
from mindspore import nn, ops, Tensor, Parameter, ParameterTuple
from mindspore.common._stub_tensor import StubTensor
from mindspore.nn.layer.conv import _Conv, _deconv_output_length
from mindspore.common.initializer import initializer, Normal, HeUniform, Uniform, _calculate_fan_in_and_fan_out
from mindspore import _checkparam as Validator
from mindspore.ops import functional as F
from mindspore.ops._primitive_cache import _get_cache_prim
from mindspore.common.parameter import PARAMETER_NAME_DEFAULT

from mindnlp._legacy.functional import einsum
from .utils.logging import get_logger
from .amp import OP_WHITE_LIST, OP_BLACK_LIST, get_global_amp

LESS_MS_2_1 = version.parse(mindspore.__version__) < version.parse('2.1.0')
LESS_MS_2_2 = version.parse(mindspore.__version__) < version.parse('2.2.0')
GE_MS_2_3 = version.parse(mindspore.__version__) >= version.parse('2.3.0')

DEVICE_TARGET = mindspore.get_context('device_target')

def int32_patch_decorator(func):
    """int32 patch on ascend"""
    def wrapper(*args, **kwargs):
        args = [arg.astype(mstype.int32) if isinstance(arg, Tensor) and arg.dtype in (mstype.int64, mstype.bool_) \
                else arg for arg in args]
        has_int64 = any(bool(isinstance(arg, Tensor) and arg.dtype in (mstype.int64, mstype.bool_)) for arg in args)
        kwargs = {k: (v.astype(mstype.int32) if isinstance(v, Tensor) and v.dtype in (mstype.int64, mstype.bool_) else v) \
                  for k, v in kwargs.items()}
        result = func(*args, **kwargs)
        if has_int64:
            result = result.astype(mstype.int64)
        return result

    return wrapper

def bool_patch_decorator(func):
    """bool patch on ascend"""
    def wrapper(*args, **kwargs):
        args = [arg.astype(mstype.int32) if isinstance(arg, Tensor) and arg.dtype == mstype.bool_ \
                else arg for arg in args]
        if isinstance(args[0], (list, tuple)):
            # for concat
            args[0] = [arg.astype(mstype.int32) if isinstance(arg, Tensor) and arg.dtype == mstype.bool_ \
                else arg for arg in args[0]]
        kwargs = {k: (v.astype(mstype.int32) if isinstance(v, Tensor) and v.dtype == mstype.bool_ else v) \
                  for k, v in kwargs.items()}
        result = func(*args, **kwargs)
        return result

    return wrapper

def bool_io_patch_decorator(func):
    """bool patch on ascend"""
    def wrapper(*args, **kwargs):
        args = [arg.astype(mstype.int32) if isinstance(arg, Tensor) and arg.dtype == mstype.bool_ \
                else arg for arg in args]
        has_bool = any(bool(isinstance(arg, Tensor) and arg.dtype == mstype.bool_) for arg in args)
        if isinstance(args[0], (list, tuple)):
            # for concat
            args[0] = [arg.astype(mstype.int32) if isinstance(arg, Tensor) and arg.dtype == mstype.bool_ \
                else arg for arg in args[0]]
        kwargs = {k: (v.astype(mstype.int32) if isinstance(v, Tensor) and v.dtype == mstype.bool_ else v) \
                  for k, v in kwargs.items()}
        result = func(*args, **kwargs)
        if has_bool:
            result = result.astype(mstype.bool_)
        return result

    return wrapper

def _get_unflatten_size(input_shape, dim, sizes):
    """
    Args:
        input_shape (Tuple[int]): The shape of the input tensor.
        dim (int): The dimension along which the unflatten operation will be performed.
        sizes (Tuple[int] or List[int]): The sizes to unflatten the tensor along the specified dimension.
    
    Returns:
        None. This function modifies the input_shape in place.
    
    Raises:
        TypeError: If sizes is not a tuple or list.
        ValueError: If sizes is empty, dim is out of range, or sizes do not multiply up to the size of dim in the input tensor.
    """
    input_rank = len(input_shape)
    if not isinstance(sizes, (tuple, list)):
        raise TypeError(f"Type of `sizes` should be `Tuple` or `List`, but got {type(sizes)}")

    if len(sizes) == 0:
        raise ValueError("`sizes` must be non-empty")

    if isinstance(dim, str):
        raise TypeError("Until Now, `dim` not support type of str in `unflatten`")

    _dim = dim
    if _dim < 0:
        _dim += input_rank

    if _dim < 0 or _dim >= input_rank:
        raise ValueError(f"`dim` should be in range [{-input_rank}, {input_rank}), but got {input_rank, dim}")

    _sizes_mul = reduce(operator.mul, list(sizes))
    if -1 not in sizes and _sizes_mul != input_shape[_dim]:
        raise ValueError(f"unflatten: Provided `sizes` {sizes} don't multiply up to the"
            f"size of dim {dim} ({input_shape[_dim]}) in the input tensor")

    out_shape = input_shape[:_dim] + tuple(sizes) + input_shape[_dim + 1:]
    return out_shape

old_op_call = ops.Primitive.__call__
def _op_call(self, *args):
    r"""
    This function modifies the input arguments based on a global flag and predefined white/black lists before calling the original function 'old_op_call'. 
    
    Args:
        self: An instance of the class containing the method.
        
    Returns:
        None: This function does not return any value directly, but it may return the outputs of the 'old_op_call' function.
    
    Raises:
        No specific exceptions are raised within this function.
    """
    GLOBAL_AMP, GLOBAL_AMP_DTYPE = get_global_amp()

    if GLOBAL_AMP:
        if GLOBAL_AMP and self.__class__.__name__ in OP_WHITE_LIST:
            args = [arg.astype(GLOBAL_AMP_DTYPE) if isinstance(arg, Tensor) \
                    else arg for arg in args]
        elif GLOBAL_AMP and self.__class__.__name__ in OP_BLACK_LIST:
            args = [arg.astype(mindspore.float32) if isinstance(arg, Tensor) \
                    else arg for arg in args]
        else:
            return old_op_call(self, *args)

        outputs = old_op_call(self, *args)

        return outputs

    return old_op_call(self, *args)


ops.Primitive.__call__ = _op_call

# For all backend
# For functional api
# matmul
# dense
def dense(input, weight, bias=None):
    """patched dense"""
    dense_ = _get_cache_prim(ops.Dense)()
    return dense_(input, weight, bias)

ops.dense = dense
# einsum
ops.einsum = einsum

def _ones(*size, dtype=None):
    r"""
    Fills a tensor of specified size with ones.
    
    Args:
        *size (tuple or list): The size of the tensor to be filled with ones.
        dtype (mindspore.dtype, optional): The data type of the tensor. Defaults to mindspore.float32.
    
    Returns:
        mindspore.tensor: A tensor filled with ones of the specified size and data type.
    
    Raises:
        None.
    
    """
    if dtype is None:
        dtype = mindspore.float32
    if isinstance(size[0], tuple):
        size = size[0]
    elif isinstance(size[0], list):
        size = tuple(size[0])
    return ops.fill(dtype, size, 1)

ops.ones = _ones

def _zeros(*size, dtype=None):
    r"""
    Args:
        *size (tuple or list): Represents the size of the output tensor to be created. If a tuple is provided, it is used as-is. If a list is provided, it is converted to a tuple.
        dtype (mindspore.dtype, optional): Data type of the elements in the output tensor. Defaults to mindspore.float32 if not provided.
    
    Returns:
        None: The function does not return a value directly, but creates and returns a tensor filled with zeros of the specified size and data type.
    
    Raises:
        None
    """
    if dtype is None:
        dtype = mindspore.float32
    if isinstance(size[0], tuple):
        size = size[0]
    elif isinstance(size[0], list):
        size = tuple(size[0])
    return ops.fill(dtype, size, 0)

ops.zeros = _zeros

# cross_entropy
def _cross_entropy(input, target, weight=None, ignore_index=-100, reduction='mean', label_smoothing=0.0):
    """
    Args:
        input (array_like): The input tensor or array of shape (N, C) where N is the batch size and C is the number of classes.
        target (array_like): The target tensor or array of shape (N,) containing the true class indices.
        weight (array_like, optional): A tensor or array of shape (C,) containing the weight for each class. Defaults to None.
        ignore_index (int, optional): Specifies a target value that is ignored and does not contribute to the loss. Defaults to -100.
        reduction (str, optional): Specifies the method used to reduce the loss. Possible values are 'none', 'mean', and 'sum'. Defaults to 'mean'.
        label_smoothing (float, optional): Specifies the label smoothing factor. Defaults to 0.0.
    
    Returns:
        None: This function does not return any value.
    
    Raises:
        ValueError: If the input and target shapes do not match.
        ValueError: If the reduction parameter has an invalid value.
        ValueError: If the label_smoothing parameter is outside the valid range [0, 1].
    """
    if weight is None:
        weight = ops.ones((input.shape[-1],), input.dtype)
    _nll_loss = _get_cache_prim(ops.NLLLoss)(reduction, ignore_index)
    class_dim = 0 if input.ndim == 1 else 1
    return _nll_loss(ops.log_softmax(input, class_dim), target, weight)[0]

# ops.cross_entropy = _cross_entropy

# for Tensor
# mean
if GE_MS_2_3 and DEVICE_TARGET == 'Ascend':
    Tensor.mean = mindspore.mint.mean
    StubTensor.mean = mindspore.mint.mean

# unfold
def _get_unfold_indices(input_shape, dimension, size, step):
    """
    Args:
        input_shape (tuple): The shape of the input data tensor.
        dimension (int): The index of the dimension along which to unfold the tensor.
        size (int): The size of the window to extract along the specified dimension.
        step (int): The step size for sliding the window along the specified dimension.
    
    Returns:
        tuple: A list of indices representing the unfolded windows along the specified dimension.
        int: The updated index of the dimension after accounting for negative values.
    
    Raises:
        ValueError: If the specified dimension is out of range relative to the input_shape.
    """
    if dimension < 0:
        dimension += len(input_shape)
    indices = []
    for i in range(0, input_shape[dimension] - size + 1, step):
        indices.append(list(range(i, i + size)))

    return indices, dimension

def unfold(self, dimension, size, step):
    """torch-like unfold"""
    _indices, _dimension = _get_unfold_indices(self.shape, dimension, size, step)
    indices = mindspore.Tensor(_indices).astype(mindspore.int32)
    output = ops.gather(self, indices, axis=_dimension)
    output = ops.moveaxis(output, _dimension + 1, -1)
    return output

Tensor.unfold = unfold
StubTensor.unfold = unfold

# var_mean
def var_mean(input, axis=None, *, correction=1, keepdims=False):
    """torch-like var_mean"""
    axis = Validator.check_and_canonicalize_axes(axis, input.ndim)
    x_mean = ops.mean(input, axis, True)
    x_sub = ops.sub(input, x_mean)
    x_pow = ops.pow(x_sub, 2)
    x_sum = ops.sum(x_pow, axis, keepdims)
    res_mean = ops.mean(input, axis, keepdims)
    nums = 1
    if not axis:
        nums = input.size
    else:
        for ax in axis:
            nums *= input.shape[ax]
    return ops.true_divide(x_sum, nums - correction), res_mean

ops.var_mean = var_mean

# std_mean
def std_mean(input, axis=None, *, correction=1, keepdims=False):
    """torch-like std_mean"""
    output = var_mean(input, axis, correction=correction, keepdims=keepdims)
    return ops.pow(output[0], 0.5), output[1]

ops.std_mean = std_mean

# masked_fill
def masked_fill(inputs, mask, value):
    """patched masked_fill"""
    masked_value = ops.fill(inputs.dtype, inputs.shape, value)
    return ops.select(mask, masked_value, inputs)

def _masked_fill(self, mask, value):
    r"""
    Fills elements of the input with a specified value, based on a provided mask.
    
    Args:
        self: The object instance.
        mask (Tensor): A boolean mask tensor of the same shape as the input.
            Only the elements where the mask is True will be filled with the specified value.
        value (Any): The value to fill the masked elements with. It can be of any type.
    
    Returns:
        None
    
    Raises:
        None
    """
    return masked_fill(self, mask, value)

ops.masked_fill = masked_fill
Tensor.masked_fill = _masked_fill
StubTensor.masked_fill = _masked_fill

# ops.std
def std(input, axis=None, ddof=0, keepdims=False):
    """patched std"""
    # Calculate mean
    mean = ops.mean(input, axis=axis, keep_dims=keepdims)

    # Squared differences from the mean
    squared_diff = (input - mean)**2

    # Sum along the specified dimension
    if axis is not None:
        sum_along_dim = ops.sum(squared_diff, dim=axis, keepdim=keepdims)
    else:
        sum_along_dim = squared_diff.sum()

    # Calculate the correction factor
    factor = 1.0 / (input.shape[axis] - ddof) if axis is not None else 1.0 / (input.size - ddof)

    # Calculate the standard deviation
    out = ops.sqrt(factor * sum_along_dim)

    return out

def _std(self, axis=None, ddof=0, keepdims=False):
    r"""
    This function calculates the standard deviation along the specified axis.
    
    Args:
        self (array_like): Input data.
        axis (int, optional): Axis along which the standard deviation is computed. Default is None, which computes the standard deviation of the flattened array.
        ddof (int, optional): Delta degrees of freedom. The divisor used in calculations is N - ddof, where N represents the number of elements. Default is 0.
        keepdims (bool, optional): If this is set to True, the axes which are reduced are left in the result as dimensions with size one. Default is False.
    
    Returns:
        None: This function returns None.
    
    Raises:
        ValueError: If 'ddof' is negative.
        TypeError: If 'axis' is not an integer.
        TypeError: If 'ddof' is not an integer.
        TypeError: If 'keepdims' is not a boolean.
    """
    return std(self, axis, ddof, keepdims)

ops.std = std
Tensor.std = _std
StubTensor.std = _std

# Tensor.__contains__
def _contains(self, key):
    r"""
    Args:
        self (object): The object instance on which the method is called.
        key (object): The key to be checked for containment in the object.
        
    Returns:
        None: This function returns None, indicating whether the key is contained in the object.
        
    Raises:
        None
    """
    eq_res = ops.equal(self, key)
    res = ops.any(eq_res)
    return bool(res)

Tensor.__contains__ = _contains
StubTensor.__contains__ = _contains

def unflatten(self, dim, sizes):
    """Tensor.unflatten"""
    out_shape = _get_unflatten_size(self.shape, dim, sizes)
    return self.reshape(out_shape)

Tensor.unflatten = unflatten
StubTensor.unflatten = unflatten

def _as_strided(self, size, stride, storage_offset=None):
    r"""
    Function _as_strided
    
    This function constructs a new view of an existing tensor with a given shape and stride.
    
    Args:
        self: The tensor object on which the method is called.
        size (tuple[int]): The desired shape of the new view.
        stride (tuple[int]): The stride of the new view.
        storage_offset (int, optional): The offset in the underlying storage of the tensor.
            Defaults to None.
    
    Returns:
        None
    
    Raises:
        RuntimeError: If the length of the size and stride parameters do not match.
    
    """
    if len(size) != len(stride):
        raise RuntimeError("mismatch in length of strides and shape.")
    index = np.arange(0, size[0]*stride[0], stride[0])
    for i in range(1, len(size)):
        tmp = np.arange(0, size[i]*stride[i], stride[i])
        index = np.expand_dims(index, -1)
        index = index + tmp
    if storage_offset is not None:
        index = index + storage_offset
    if index.size == 0:
        input_indices = mindspore.numpy.empty(index.shape, dtype=mstype.int32)
    else:
        input_indices = Tensor(index)
    out = ops.gather(self.reshape(-1), input_indices, 0)
    return out

Tensor.as_strided = _as_strided
StubTensor.as_strided = _as_strided

def _nonzero(self, as_tuple=False):
    """
    Args:
        self (Tensor): The input tensor to find nonzero elements in.
        as_tuple (bool, optional): Whether to return the result as a tuple of tensors. Default is False.
    
    Returns:
        None: The function returns the nonzero elements in the input tensor as specified.
    
    Raises:
        TypeError: If the input tensor is not of type mstype.bool_.
    """
    if self.dtype == mstype.bool_:
        self = self.astype(mstype.int64)
    outs = ops.nonzero(self)
    if as_tuple:
        outs = ops.tensor_split(outs, self.ndim, -1)
        outs = tuple(out.squeeze(-1) for out in outs)
    return outs

Tensor.nonzero = _nonzero
StubTensor.nonzero = _nonzero

def _expand(self, *size):
    """
    Expands the input array to a larger size.
    
    Args:
        self (array_like): The input array to be expanded.
        *size (int or tuple of ints): The size to which the input array will be expanded. If a single integer is provided, the array will be expanded to a shape with that size along all dimensions.
    
    Returns:
        None. The function modifies the input array in place.
    
    Raises:
        None.
    """
    if len(size) == 1:
        size = size[0]
    return ops.broadcast_to(self, size)

Tensor.expand = _expand
StubTensor.expand = _expand

if LESS_MS_2_2:
    mindspore.bfloat16 = None
    def eq(self, other):
        """patched eq"""
        return ops.equal(self, other)
    Tensor.eq = eq
    StubTensor.eq = eq

    def _item(self):
        return self.asnumpy().item()
    Tensor.item = _item
    StubTensor.item = _item

    def _tolist(self):
        return self.asnumpy().tolist()
    Tensor.tolist = _tolist
    StubTensor.tolist = _tolist

mindspore.tensor = mindspore.Tensor
ops.prod = bool_patch_decorator(ops.prod)
def _prod(self, axis=None, keep_dims=False):
    """
    This function calculates the product of elements along a specified axis.
    
    Args:
        self: The input tensor.
        axis (int, optional): The axis along which the product is calculated. Defaults to None, which calculates the product of all elements.
        keep_dims (bool, optional): If True, retains reduced dimensions with length 1. Defaults to False.
    
    Returns:
        None. The product of the elements along the specified axis is returned.
    
    Raises:
        ValueError: If the specified axis is out of range or invalid.
        TypeError: If the input tensor is not valid.
    """
    return ops.prod(self, axis, keep_dims)
Tensor.prod = _prod
StubTensor.prod = _prod

def _eq(self, other):
    r"""
    Function to compare the equality of two objects.
    
    Args:
        self (Tensor): The first object to compare.
        other (int, float, Tensor): The second object to compare. Must be an integer, float, or Tensor.
    
    Returns:
        None: This function does not return any value, but performs the equality comparison operation.
    
    Raises:
        None
    """
    if not isinstance(other, (int, float, Tensor)):
        return False
    if isinstance(other, Tensor) and self.shape != other.shape:
        return False
    if id(self) == id(other):
        return True
    # bool type is not supported for `Equal` operator in backend.
    if self.dtype == mstype.bool_ or (isinstance(other, Tensor) and other.dtype == mstype.bool_):
        self = self.to(mstype.int32)
        other = other.to(mstype.int32)
    return ops.eq(self, other)

Parameter.__eq__ = _eq


old_repeat = Tensor.repeat
def new_repeat_interleave(input, repeats, axis=None):
    """new repeat_interleave"""
    if axis is None:
        input = input.reshape(-1)
        axis = 0
    if isinstance(repeats, Tensor):
        repeats = repeats.asnumpy().tolist()
    output = old_repeat(input, repeats, axis)
    return output

ops.repeat_interleave = bool_io_patch_decorator(new_repeat_interleave)
def _repeat_interleave(self, repeats, dim):
    r"""
    Args:
        self (object): The object instance on which the method is called.
        repeats (int): The number of times to repeat the operation.
        dim (int): The axis along which to repeat the operation.
    
    Returns:
        None: This function does not return any value.
    
    Raises:
        None
    """
    return old_repeat(self, repeats, axis=dim)

Tensor.repeat_interleave = _repeat_interleave
StubTensor.repeat_interleave = _repeat_interleave

def _repeat(self, *sizes):
    r"""
    This function repeats the input tensor along each dimension according to the given sizes.
    
    Args:
    - self (tensor): The input tensor to be repeated.
    - *sizes: Variable number of integers representing the number of repetitions along each dimension.
    
    Returns:
    None: This function does not return any value explicitly.
    
    Raises:
    N/A
    """
    return ops.tile(self, tuple(sizes))

Tensor.repeat = _repeat
StubTensor.repeat = _repeat

if version.parse(mindspore.__version__) < version.parse('2.3.0'):
    def _stride(self):
        strides = self.strides
        return tuple(stride // 4 for stride in strides)
    Tensor.stride = _stride
    StubTensor.stride = _stride

def _ne(self, other):
    r"""
    This function is used to check if the given 'self' object is not equal to the 'other' object.
    
    Args:
        self: The first object to compare. (Type: Any)
        other: The second object to compare against. (Type: Any)
    
    Returns:
        None: This function does not return any value.
    
    Raises:
        None: This function does not raise any exceptions.
    """
    return ops.ne(self, other)

Tensor.__ne__ = _ne
StubTensor.__ne__ = _ne

# Ascend only
if DEVICE_TARGET == 'Ascend':
    # cumsum
    ops.cumsum = int32_patch_decorator(ops.cumsum)
    def _cumsum(self, axis):
        return ops.cumsum(self, axis)
    Tensor.cumsum = _cumsum
    StubTensor.cumsum = _cumsum
    # prod
    ops.prod = bool_patch_decorator(ops.prod)
    def prod(self, axis=None, keep_dims=False):
        """patched prod on Ascend"""
        return bool_patch_decorator(ops.prod)(self, axis, keep_dims)
    Tensor.prod = prod
    StubTensor.prod = prod
    # bitwise_or
    ops.bitwise_or = bool_patch_decorator(ops.bitwise_or)
    def bitwise_or(self, other):
        """patched bitwise_or on Ascend"""
        return bool_patch_decorator(ops.bitwise_or)(self, other)
    Tensor.bitwise_or = bitwise_or
    Tensor.__or__ = bitwise_or
    StubTensor.bitwise_or = bitwise_or
    StubTensor.__or__ = bitwise_or
    # bitwise_xor
    ops.bitwise_xor = bool_patch_decorator(ops.bitwise_xor)
    def bitwise_xor(self, other):
        """patched bitwise_xor on Ascend"""
        return bool_patch_decorator(ops.bitwise_xor)(self, other)
    Tensor.bitwise_xor = bitwise_xor
    Tensor.__xor__ = bitwise_xor
    StubTensor.bitwise_xor = bitwise_xor
    StubTensor.__xor__ = bitwise_xor
    # bitwise_and
    ops.bitwise_and = bool_patch_decorator(ops.bitwise_and)
    def bitwise_and(self, other):
        """patched bitwise_and on Ascend"""
        return bool_patch_decorator(ops.bitwise_and)(self, other)
    Tensor.bitwise_and = bitwise_and
    Tensor.__and__ = bitwise_and
    StubTensor.bitwise_and = bitwise_and
    StubTensor.__and__ = bitwise_and
    # isclose
    ops.isclose = partial(ops.isclose, equal_nan=True)
    # concat
    ops.cat = bool_patch_decorator(ops.cat)
    ops.concat = bool_patch_decorator(ops.concat)

def randperm(n, seed=0, offset=0, dtype=mstype.int64):
    """randperm"""
    if DEVICE_TARGET == 'CPU':
        randperm_v2_op = _get_cache_prim(ops.RandpermV2)(seed, offset, dtype)
        return randperm_v2_op(n)
    randperm_op = _get_cache_prim(ops.Randperm)(max_length=n, dtype=dtype)
    return randperm_op(mindspore.tensor([n]))

ops.randperm = randperm

# GPU only
def custom_multinomial(probabilities, num_samples, replacement=False):
    """custom multinomial"""
    if replacement:
        # with replacement
        if LESS_MS_2_2:
            cumulative_probs = mindspore.tensor(np.cumsum(probabilities.asnumpy(), -1), probabilities.dtype)
        else:
            cumulative_probs = ops.cumsum(probabilities, axis=-1)
        uniform_samples = ops.rand(probabilities.shape[:-1] + (num_samples,))
        if cumulative_probs.dtype == mindspore.float16:
            cumulative_probs = cumulative_probs.astype(mindspore.float32)
        samples = ops.searchsorted(cumulative_probs, uniform_samples, right=True)
    else:
        # without replacement
        n_dist = 1
        if probabilities.ndim > 1:
            n_dist = probabilities.shape[-2]
        random_uniform = ops.rand((n_dist * probabilities.shape[-1],))
        if n_dist != 1:
            random_uniform = random_uniform.reshape(n_dist, probabilities.shape[-1])

        vals = ops.div(ops.log(random_uniform), probabilities + 1e-10)
        _, samples = ops.top_k(vals, num_samples)

    return samples.astype(mstype.int64)

ops.multinomial = custom_multinomial

# For Cells
class Dense(nn.Cell):
    """patched Dense"""
    def __init__(self,
                 in_channels,
                 out_channels,
                 has_bias=True,
                 dtype=mstype.float32):
        """Initialize Dense."""
        super().__init__()
        self.in_channels = Validator.check_positive_int(
            in_channels, "in_channels", self.cls_name)
        self.out_channels = Validator.check_positive_int(
            out_channels, "out_channels", self.cls_name)
        self.has_bias = Validator.check_bool(
            has_bias, "has_bias", self.cls_name)

        self.weight = Parameter(initializer(
            HeUniform(math.sqrt(5)), [out_channels, in_channels], dtype=dtype), name="weight")

        self.bias = None
        if self.has_bias:
            fan_in, _ = _calculate_fan_in_and_fan_out(self.weight.shape)
            bound = 1 / math.sqrt(fan_in)
            self.bias = Parameter(initializer(
                Uniform(bound), [out_channels], dtype=dtype), name="bias")

    def construct(self, x):
        r"""
        This method constructs the output of a dense layer operation based on the input tensor 'x' using the weights and biases of the Dense class instance.
        
        Args:
            self (object): The instance of the Dense class.
            x (ndarray): The input tensor of shape (batch_size, input_dim) or (batch_size, *, input_dim), where * represents additional dimensions. 
        
        Returns:
            None: This method does not return any value but modifies the input tensor 'x' in-place to produce the output of the dense layer operation.
        
        Raises:
            ValueError: If the input tensor 'x' does not have the required shape for matrix multiplication with the weight matrix.
            ValueError: If the input tensor 'x' and the weight matrix dimensions are incompatible for matrix multiplication.
            ValueError: If the input tensor 'x' and bias tensor dimensions are incompatible for addition.
        """
        if LESS_MS_2_2:
            x_shape = x.shape
            if len(x_shape) != 2:
                x = x.reshape(-1, x.shape[-1])
            x = ops.matmul(x, self.weight.T)
            if self.has_bias:
                x = ops.add(x, self.bias)
            if len(x_shape) != 2:
                out_shape = x_shape[:-1] + (x.shape[-1],)
                x = x.reshape(out_shape)
            return x
        return ops.dense(x, self.weight, self.bias)

    def extend_repr(self):
        r"""
        This method extends the string representation of the Dense class instance.
        
        Args:
            self (Dense): The instance of the Dense class.
            
        Returns:
            None: This method does not return any value.
        
        Raises:
            None
        """
        s = f'input_channels={self.in_channels}, output_channels={self.out_channels}'
        if self.has_bias:
            s += f', has_bias={self.has_bias}'
        return s

class Embedding(nn.Cell):
    """patched Embedding"""
    def __init__(self, vocab_size, embedding_size, padding_idx=None, use_one_hot=False, dtype=mstype.float32):
        """Initialize Embedding."""
        super().__init__()
        self.vocab_size = Validator.check_value_type('vocab_size', vocab_size, [int], self.cls_name)
        self.embedding_size = Validator.check_value_type('embedding_size', embedding_size, [int], self.cls_name)
        Validator.check_value_type('use_one_hot', use_one_hot, [bool], self.cls_name)
        Validator.check_subclass("dtype", dtype, mstype.number_type, self.cls_name)
        self.use_one_hot = use_one_hot
        self.dtype = dtype
        self.padding_idx = padding_idx
        self.weight = Parameter(initializer(Normal(1.0), [vocab_size, embedding_size]), name='weight')
        if self.padding_idx and self.weight.init_flag:
            self.weight[self.padding_idx] = 0

    def construct(self, ids):
        r"""
        Constructs an embedding tensor based on the given input IDs.
        
        Args:
            self (Embedding): An instance of the Embedding class.
            ids (ndarray): A numpy array containing the input IDs. The shape of the array should be (batch_size, sequence_length).
        
        Returns:
            ndarray: An embedding tensor with the shape (batch_size, sequence_length, embedding_size).
        
        Raises:
            TypeError: If the 'ids' parameter is not a numpy array.
            ValueError: If the shape of the 'ids' array is not (batch_size, sequence_length).
        """
        if DEVICE_TARGET == 'Ascend' and GE_MS_2_3:
            return ops.embedding(ids, self.weight, self.padding_idx)

        out_shape = ids.shape + (self.embedding_size,)
        flat_ids = ids.reshape((-1,))

        if self.use_one_hot:
            one_hot_ids = ops.one_hot(flat_ids, self.vocab_size)
            output_for_reshape = ops.matmul(one_hot_ids, self.weight)
        else:
            output_for_reshape = ops.gather(self.weight, flat_ids, 0)

        output = output_for_reshape.reshape(out_shape)
        return output

    def extend_repr(self):
        r"""
        This method extends the representation of the Embedding class.
        
        Args:
            self: The instance of the Embedding class.
            
        Returns:
            None: This method does not return any value.
        
        Raises:
            None
        """
        return f'vocab_size={self.vocab_size}, embedding_size={self.embedding_size}, use_one_hot={self.use_one_hot}, ' \
            f'weight={self.weight}, dtype={self.dtype}, padding_idx={self.padding_idx}'



class Conv1d(_Conv):
    """patched Conv1d"""
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 pad_mode='same',
                 padding=0,
                 dilation=1,
                 group=1,
                 has_bias=True):
        """Initialize Conv1d."""
        Validator.check_value_type("kernel_size", kernel_size, [int], self.cls_name)
        Validator.check_value_type("stride", stride, [int], self.cls_name)
        Validator.check_value_type("padding", padding, [int], self.cls_name)
        Validator.check_value_type("dilation", dilation, [int], self.cls_name)
        Validator.check_int(kernel_size, 1, Validator.GE, 'kernel_size', self.cls_name)
        Validator.check_int(stride, 1, Validator.GE, 'stride', self.cls_name)
        Validator.check_non_negative_int(padding, 'padding', self.cls_name)
        Validator.check_int(dilation, 1, Validator.GE, 'dilation', self.cls_name)
        Validator.check_positive_int(group, 'group', self.cls_name)
        if not (in_channels % group == 0 and out_channels % group == 0):
            raise ValueError(f"The argument 'group' should be divisible by 'in_channels' " \
                             f"and 'out_channels', but got group:{group}, in_channels:{in_channels}, " \
                             f"out_channels:{out_channels}.")

        super().__init__(
            in_channels,
            out_channels,
            (kernel_size,),
            (1, stride),
            pad_mode,
            padding,
            (1, dilation),
            group,
            has_bias,
            None,
            None)
        self.padding = (0, 0, padding, padding)
        self.padding = (0, 0, padding, padding)
        Validator.check_string(pad_mode, ['valid', 'same', 'pad'], 'pad_mode', self.cls_name)
        self.conv2d = ops.Conv2D(out_channel=self.out_channels,
                               kernel_size=(1, kernel_size),
                               mode=1,
                               pad_mode=self.pad_mode,
                               pad=self.padding,
                               stride=self.stride,
                               dilation=self.dilation,
                               group=self.group)

        self.stride = (stride,)
        self.dilation = (dilation,)

    def construct(self, x):
        r"""
        Method to construct a 1D convolutional layer.
        
        Args:
            self (Conv1d): The instance of the Conv1d class.
            x (Tensor): Input tensor to be processed. Should have shape (batch_size, in_channels, sequence_length).
        
        Returns:
            None: This method does not return any value.
        
        Raises:
            ValueError: If the input tensor x does not have the required shape.
            TypeError: If any of the operations encounter incompatible data types.
            RuntimeError: If the method encounters any runtime issues during processing.
        """
        x = x.expand_dims(2)
        if DEVICE_TARGET == 'Ascend':
            x_dtype = x.dtype
            output = self.conv2d(x.astype(mindspore.float16), self.weight.expand_dims(2).astype(mindspore.float16))
        else:
            output = self.conv2d(x, self.weight.expand_dims(2))
        if DEVICE_TARGET == 'Ascend':
            output = output.astype(x_dtype)

        if self.has_bias:
            output = ops.bias_add(output, self.bias)

        output = output.squeeze(2)
        return output


class Conv2d(_Conv):
    r"""
    2D convolution layer.
    """

    def __init__(self,
                in_channels,
                out_channels,
                kernel_size,
                stride=1,
                pad_mode='same',
                padding=0,
                dilation=1,
                group=1,
                has_bias=False,
                weight_init=None,
                bias_init=None,
                data_format='NCHW',
                dtype=mstype.float32):
        """Initialize Conv2d."""
        kernel_size = Validator.twice(kernel_size)
        stride = Validator.twice(stride)
        self._dilation = dilation
        dilation = Validator.twice(dilation)
        Validator.check_positive_int(group, 'group', self.cls_name)
        if not (in_channels % group == 0 and out_channels % group == 0):
            raise ValueError(f"The argument 'group' should be divisible by 'in_channels' " \
                             f"and 'out_channels', but got group:{group}, in_channels:{in_channels}, " \
                             f"out_channels:{out_channels}.")
        super(Conv2d, self).__init__(
            in_channels,
            out_channels,
            kernel_size,
            stride,
            pad_mode,
            padding,
            dilation,
            group,
            has_bias,
            weight_init,
            bias_init,
            data_format,
            dtype=dtype)
        self.conv2d = ops.Conv2D(out_channel=self.out_channels,
                               kernel_size=self.kernel_size,
                               mode=1,
                               pad_mode=self.pad_mode,
                               pad=self.padding,
                               stride=self.stride,
                               dilation=self.dilation,
                               group=self.group,
                               data_format=self.data_format)
        self.bias_add = ops.BiasAdd(data_format=self.data_format)

    def construct(self, x):
        if DEVICE_TARGET == 'Ascend':
            x_dtype = x.dtype
            output = self.conv2d(x.astype(mindspore.float16), self.weight.astype(mindspore.float16))
        else:
            output = self.conv2d(x, self.weight)
        if DEVICE_TARGET == 'Ascend':
            output = output.astype(x_dtype)
        if self.has_bias:
            output = self.bias_add(output, self.bias)
        return output


class Conv1dTranspose(_Conv):
    """patched Conv1dTranspose"""
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 pad_mode='same',
                 padding=0,
                 dilation=1,
                 group=1,
                 has_bias=True,
                 weight_init='zeros',
                 bias_init='zeros',
                 dtype=mstype.float32):
        """Initialize Conv1dTranspose."""
        Validator.check_value_type("kernel_size", kernel_size, [int], self.cls_name)
        Validator.check_value_type("stride", stride, [int], self.cls_name)
        Validator.check_value_type("padding", padding, [int], self.cls_name)
        Validator.check_value_type("dilation", dilation, [int], self.cls_name)
        Validator.check_int(kernel_size, 1, Validator.GE, 'kernel_size', self.cls_name)
        Validator.check_int(stride, 1, Validator.GE, 'stride', self.cls_name)
        Validator.check_non_negative_int(padding, 'padding', self.cls_name)
        Validator.check_int(dilation, 1, Validator.GE, 'dilation', self.cls_name)
        # out_channels and in_channels swap.
        # cause Conv2DBackpropInput's out_channel refers to Conv2D's out_channel,
        # then Conv1dTranspose's out_channel refers to Conv2DBackpropInput's in_channel.
        super().__init__(
            in_channels,
            out_channels,
            (kernel_size,),
            (1, stride,),
            pad_mode,
            padding,
            (1, dilation,),
            group,
            has_bias,
            None,
            None,
            transposed=True,
            dtype=dtype)
        self.padding = (0, 0, padding, padding)
        self.in_channels = in_channels
        self.out_channels = out_channels
        Validator.check_string(pad_mode, ['valid', 'same', 'pad'], 'pad_mode', self.cls_name)
        self.is_valid = self.pad_mode == 'valid'
        self.is_same = self.pad_mode == 'same'
        self.is_pad = self.pad_mode == 'pad'

        self.kernel_size = (kernel_size,)
        self.stride = (stride,)
        self.dilation = (dilation,)
        # cause Conv2DBackpropInput's out_channel refers to Conv2D's out_channel.
        self.conv2d_transpose = ops.Conv2DBackpropInput(out_channel=in_channels,
                                                      kernel_size=(1, kernel_size,),
                                                      mode=1,
                                                      pad_mode=pad_mode,
                                                      pad=self.padding,
                                                      stride=(1, stride,),
                                                      dilation=(1, dilation,),
                                                      group=group)

    def construct(self, x):
        r"""
        Constructs a transposed 1D convolution operation.
        
        Args:
            self (Conv1dTranspose): An instance of Conv1dTranspose class.
            x (Tensor): The input tensor to be transposed. Should have shape (batch_size, in_channels, width).
        
        Returns:
            None: This method does not return any value directly. The transposed convolution operation is applied in-place on the input tensor x.
        
        Raises:
            ValueError: If the dimensions of the input tensor x are incorrect for the transposed convolution operation.
            RuntimeError: If an error occurs during the transposed convolution computation.
        """
        x = x.expand_dims(2)
        n, _, h, w = x.shape

        h_out = _deconv_output_length(self.is_valid, self.is_same, self.is_pad, h, 1,
                                      1, 1, self.padding[0] + self.padding[1])
        w_out = _deconv_output_length(self.is_valid, self.is_same, self.is_pad, w, self.kernel_size[0],
                                      self.stride[0], self.dilation[0], self.padding[2] + self.padding[3])
        output = self.conv2d_transpose(x, self.weight.expand_dims(2), (n, self.out_channels, h_out, w_out))
        if self.has_bias:
            output = ops.bias_add(output, self.bias)
        output = output.squeeze(2)
        return output


class LayerNorm(nn.Cell):
    r"""
    Applies Layer Normalization over a mini-batch of inputs.
    """
    def __init__(self,
                 normalized_shape,
                 begin_norm_axis=-1,
                 begin_params_axis=-1,
                 gamma_init='ones',
                 beta_init='zeros',
                 epsilon=1e-5,
                 eps=None,
                 dtype=mstype.float32,
                 elementwise_affine=True
                 ):
        """Initialize LayerNorm."""
        super().__init__()
        if isinstance(normalized_shape, int):
            normalized_shape = [normalized_shape]
        if not isinstance(normalized_shape, (tuple, list)):
            raise TypeError(f"For '{self.cls_name}', the type of 'normalized_shape' must be tuple[int] or list[int], "
                            f"but got {normalized_shape} and the type is {type(normalized_shape)}.")
        if not normalized_shape:
            raise ValueError(
                f"Expected normalized_shape to be at least 1-dimensional, i.e., containing at "
                f"least one element, but got normalized_shape = {normalized_shape}"
            )

        self.normalized_shape = normalized_shape
        self.begin_norm_axis = begin_norm_axis
        self.begin_params_axis = begin_params_axis
        if eps and epsilon == 1e-5:
            self.epsilon = eps
        else:
            self.epsilon = epsilon

        self.weight = Parameter(initializer(
            gamma_init, normalized_shape, dtype=dtype), name="weight")
        self.bias = Parameter(initializer(
            beta_init, normalized_shape, dtype=dtype), name="bias")
        self.layer_norm = ops.LayerNorm(begin_norm_axis=self.begin_norm_axis,
                                      begin_params_axis=self.begin_params_axis,
                                      epsilon=self.epsilon)
        self.elementwise_affine = elementwise_affine

    def construct(self, input_x):
        r"""
        Constructs the normalized output tensor based on the input tensor using Layer Normalization.
        
        Args:
            self (object): The instance of the LayerNorm class.
            input_x (tensor): The input tensor to be normalized.
            
        Returns:
            None: This method does not return any value, as the normalized tensor is directly stored within the instance.
        
        Raises:
            ValueError: If the 'elementwise_affine' attribute is not a boolean.
            TypeError: If the weights and biases cannot be cast to the same data type as the input tensor.
            ValueError: If the normalized shape of the input tensor does not match the expected shape for the layer normalization.
        """
        if self.elementwise_affine:
            y, _, _ = self.layer_norm(input_x, self.weight.astype(input_x.dtype), self.bias.astype(input_x.dtype))
        else:
            y, _, _ = self.layer_norm(input_x, ops.ones(self.normalized_shape, input_x.dtype),
                                      ops.zeros(self.normalized_shape, input_x.dtype),)
        return y

    def extend_repr(self):
        r"""
        Method to extend the representation of the LayerNorm class instance.
        
        Args:
            self (LayerNorm): The instance of the LayerNorm class.
                - Represents the current instance of the LayerNorm class.
                - Type: LayerNorm class instance.
        
        Returns:
            None
                - This method does not return any value.
        
        Raises:
            None
                - This method does not raise any exceptions.
        """
        return f'normalized_shape={self.normalized_shape}, begin_norm_axis={self.begin_norm_axis}, ' \
               f'begin_params_axis={self.begin_params_axis}, weight={self.weight}, bias={self.bias}'

class BatchNorm1d(nn.Cell):
    """Batch Normalization base class."""
    def __init__(self,
                 num_features,
                 eps=1e-5,
                 momentum=0.9,
                 affine=True,
                 weight_init='ones',
                 bias_init='zeros',
                 moving_mean_init='zeros',
                 moving_var_init='ones',
                 use_batch_statistics=None,
                 dtype=mstype.float32):
        """Initialize _BatchNorm."""
        super().__init__()
        if num_features < 1:
            raise ValueError(f"For '{self.cls_name}', the 'num_features' must be at least 1, but got {num_features}.")

        if momentum < 0 or momentum > 1:
            raise ValueError(f"For '{self.cls_name}', the 'momentum' must be a number in range [0, 1], "
                             f"but got {momentum}.")
        self.use_batch_statistics = use_batch_statistics
        if self.use_batch_statistics is not None and not isinstance(self.use_batch_statistics, bool):
            raise ValueError(f"For '{self.cls_name}', the 'use_batch_statistics' must be a boolean value or None,"
                             f" but got {use_batch_statistics}.")
        self.num_features = num_features
        self.eps = eps
        self.moving_mean_init = moving_mean_init
        self.moving_var_init = moving_var_init
        self.running_mean = Parameter(initializer(
            moving_mean_init, num_features, dtype=dtype), name="running_mean", requires_grad=False)
        self.running_var = Parameter(initializer(
            moving_var_init, num_features, dtype=dtype), name="running_var", requires_grad=False)
        self.weight = Parameter(initializer(
            weight_init, num_features, dtype=dtype), name="weight", requires_grad=affine)
        self.bias = Parameter(initializer(
            bias_init, num_features, dtype=dtype), name="bias", requires_grad=affine)

        self.momentum = 1.0 - momentum

        self.bn_train = ops.BatchNorm(is_training=True,
                                    epsilon=self.eps,
                                    momentum=self.momentum)

        self.bn_infer = ops.BatchNorm(is_training=False, epsilon=self.eps)

    def construct(self, x):
        r"""
        This method constructs the batch normalization for 1D input.
        
        Args:
            self (object): The instance of the BatchNorm1d class.
            x (tensor): The input tensor to be normalized.
        
        Returns:
            None: This method does not return any value.
        
        Raises:
            None
        """
        if self.use_batch_statistics is None:
            if self.training:
                return self.bn_train(x,
                                     self.weight,
                                     self.bias,
                                     self.running_mean,
                                     self.running_var)[0]
            if not self.training:
                return self.bn_infer(x,
                                     self.weight,
                                     self.bias,
                                     self.running_mean,
                                     self.running_var)[0]

        if self.use_batch_statistics:
            return self.bn_train(x,
                                 self.weight,
                                 self.bias,
                                 self.running_mean,
                                 self.running_var)[0]

        return self.bn_infer(x,
                             self.weight,
                             self.bias,
                             self.running_mean,
                             self.running_var)[0]

    def extend_repr(self):
        r"""
        This method extends the string representation of the BatchNorm1d class instance.
        
        Args:
            self (BatchNorm1d): The instance of the BatchNorm1d class.
                It represents the BatchNorm1d layer with parameters like num_features, eps, momentum, weight, bias, running_mean, and running_var.
        
        Returns:
            None: This method does not return any value.
        
        Raises:
            None
        """
        return f'num_features={self.num_features}, eps={self.eps}, momentum={1.0 - self.momentum}, ' \
               f'weight={self.weight}, bias={self.bias}, running_mean={self.running_mean}, running_var={self.running_var}'


def _half(self):
    """patched nn.Cell.half"""
    # self.to_float(mindspore.float16), fix t5 fp16 inference error
    for _, param in self.parameters_and_names():
        if param.dtype in (mindspore.float16, mindspore.float32, mindspore.bfloat16):
            # param.set_dtype(mindspore.float16) # set_dtype is useless if Parameter copied from host to device(just be used).
            param.set_data(param.astype(mindspore.float16)) # this is a bug that MindSpore just use new pointer of incoming Tensor
            param.init_data()
    return self

nn.Cell.half = _half

def _float(self):
    """patched nn.Cell.float"""
    # self.to_float(mindspore.float32)
    for _, param in self.parameters_and_names():
        if param.dtype in (mindspore.float16, mindspore.float32, mindspore.bfloat16):
            param.set_data(param.astype(mindspore.float32))
            # param.set_dtype(mindspore.float32)
    return self

nn.Cell.float = _float


if not LESS_MS_2_2:
    def _bfloat16(self):
        """patched nn.Cell.bfloat16"""
        self.to_float(mindspore.bfloat16)
        for _, param in self.parameters_and_names():
            if param.dtype in (mindspore.float16, mindspore.float32, mindspore.bfloat16):
                param.set_dtype(mindspore.bfloat16)
        return self

    nn.Cell.bfloat16 = _bfloat16


def _check_cell_flags_in_pynative(self):
    r"""
    This function checks the cell flags in the PyNative environment.
    
    Args:
        self: The instance of the class.
    
    Returns:
        None. This function does not return any value.
    
    Raises:
        No exceptions are raised by this function.
    """

nn.Cell._check_cell_flags_in_pynative = _check_cell_flags_in_pynative

def _update_parameters_name(self, prefix='', recurse=True):
    r"""
    Updates the parameter names with the specified prefix.
    
    Args:
        self (object): The instance of the class.
        prefix (str): The prefix to be added to the parameter names.
        recurse (bool): Indicates whether to recursively update nested parameters.
    
    Returns:
        None: This function does not return any value.
    
    Raises:
        None: This function does not raise any exceptions.
    """
    for name, param in self.parameters_and_names(expand=recurse):
        if prefix != '':
            param.is_init = False
        if param.name in name: # for tied weight
            param.name = prefix + name

nn.Cell.update_parameters_name = _update_parameters_name

def _cells_and_names(self, name_prefix=''):
    """
    Returns an iterator over all cells in the network, including the cell's name and itself.
    """
    yield name_prefix, self

    for name, cell in self._cells.items():
        if cell:
            cells_name_prefix = name
            if name_prefix:
                cells_name_prefix = name_prefix + '.' + cells_name_prefix
            yield from cell.cells_and_names(cells_name_prefix)

nn.Cell.cells_and_names = _cells_and_names

def _run_forward_hook(self, inputs, output):
    """
    Running forward hook function registered on Cell object.

    Args:
        inputs: The input objects of Cell object.
        output: The output object of Cell object.

    Returns:
        - **output** - New output object or none.

    Supported Platforms:
    ``Ascend`` ``GPU`` ``CPU``
    """
    cell_id = self.cls_name + "(" + str(id(self)) + ")"
    for fn in self._forward_hook.values():
        ret = fn(self, inputs, output)
        if ret is not None:
            output = ret
    return output

nn.Cell._run_forward_hook = _run_forward_hook


def parameters_dict(self, recurse=True):
    """
    fix ignore tied weights
    """
    param_dict = OrderedDict()
    for name, param in self.parameters_and_names(expand=recurse):
        param_dict[name] = param
    return param_dict


logger = get_logger()
def cell_load_state_dict(self, parameter_dict, strict=False):
    """
    Load parameters into network, return parameter list that are not loaded in the network.
    """
    if not isinstance(parameter_dict, dict):
        logger.critical("Failed to combine the net and the parameters.")
        msg = ("For 'load_state_dict', the argument 'parameter_dict' should be a dict, "
            "but got {}.".format(type(parameter_dict)))
        raise TypeError(msg)

    for key, value in parameter_dict.items():
        if not isinstance(key, str) or not isinstance(value, (Parameter, str, list)):
            logger.critical("Load parameters into net failed.")
            msg = ("For 'parameter_dict', the element in the argument 'parameter_dict' should be a "
                "'str' and 'Parameter' , but got {} and {}.".format(type(key), type(value)))
            raise TypeError(msg)

    param_not_load = []
    ckpt_not_load = list(parameter_dict.keys())
    for name, param in self.parameters_and_names():
        if name in parameter_dict:
            new_param = parameter_dict[name]
            param.set_data(new_param)
            ckpt_not_load.remove(name)
        else:
            param_not_load.append(name)

    return param_not_load, ckpt_not_load

nn.Cell.load_state_dict = cell_load_state_dict
nn.Cell.parameters_dict = parameters_dict


nn.LayerNorm = LayerNorm
nn.Conv1d = Conv1d
nn.Conv2d = Conv2d
nn.Conv1dTranspose = Conv1dTranspose
nn.Embedding = Embedding
nn.Dense = Dense
nn.BatchNorm1d = BatchNorm1d
nn.BatchNorm2d = BatchNorm1d


GroupNorm_original = nn.GroupNorm

class GroupNorm_hijack(GroupNorm_original):
    r"""
    Group Normalization over a mini-batch of inputs.
    """
    def __init__(self, num_groups, num_channels, eps=0.00001, affine=True, gamma_init='ones', beta_init='zeros', dtype=mstype.float32):
        r"""
        Initializes an instance of the GroupNorm_hijack class.
        
        Args:
            self: The object itself.
            num_groups (int): The number of groups to divide the channels into.
            num_channels (int): The number of input channels.
            eps (float, optional): A small value added to the denominator for numerical stability. Default is 1e-05.
            affine (bool, optional): If True, apply learnable affine transformation to the normalized output. Default is True.
            gamma_init (str, optional): The initialization method for the gamma parameter. Default is 'ones'.
            beta_init (str, optional): The initialization method for the beta parameter. Default is 'zeros'.
            dtype (mstype, optional): The data type of the input tensors. Default is mstype.float32.
        
        Returns:
            None. This method does not return any value.
        
        Raises:
            None.
        """
        super().__init__(num_groups, num_channels, eps, affine, gamma_init, beta_init, dtype)
        self.weight = Parameter(self.gamma.data, name='weight')
        self.bias = Parameter(self.beta.data, name='bias')
        self.reduce_mean=ops.ReduceMean(keep_dims=True)
        self.reduce_sum=ops.ReduceSum(keep_dims=True)
        self.sqrt = ops.Sqrt()
        del self.gamma
        del self.beta

    def _cal_output(self, x):
        r"""
        Calculates the output of GroupNorm_hijack.
        
        Args:
            self (GroupNorm_hijack): An instance of the GroupNorm_hijack class.
            x (tensor): The input tensor of shape (batch, channel, height, width).
        
        Returns:
            None
        
        Raises:
            ValueError: If the number of channels in the input tensor is not equal to the number of channels specified in GroupNorm_hijack.
            ZeroDivisionError: If the number of groups is zero.
            TypeError: If the input tensor is not a valid tensor object.
        
        """
        batch, channel, height, width = F.shape(x)
        self._channel_check(channel, self.num_channels, self.cls_name)
        x = F.reshape(x, (batch, self.num_groups, -1))
        mean = self.reduce_mean(x, 2)
        var = F.div(self.reduce_sum(F.square(F.sub(x, mean)), 2), (channel * height * width / self.num_groups))
        std_ = self.sqrt(var + self.eps)
        x = F.div(F.sub(x, mean), std_)
        x = F.reshape(x, (batch, channel, height, width))
        output = F.add(x * F.reshape(self.weight, (-1, 1, 1)), F.reshape(self.bias, (-1, 1, 1)))
        return output

    def construct(self, x:Tensor) -> Tensor:
        r"""
        Method to construct a tensor with additional processing steps based on its shape.
        
        Args:
            self (object): The instance of the GroupNorm_hijack class.
            x (Tensor): The input tensor to be processed. Must be a 3D tensor.
        
        Returns:
            Tensor: The processed output tensor after applying the necessary operations.
        
        Raises:
            N/A
        """
        is_3d_tensor = len(x.shape) == 3        # support 3D tensors [B, C, L]
        if is_3d_tensor:
            x = x.unsqueeze(-1)
        o = super().construct(x)
        if is_3d_tensor:
            o = o.squeeze(-1)
        return o

nn.GroupNorm = GroupNorm_hijack

def state_dict(self):
    """Returns the state of the scheduler as a :class:`dict`.

    It contains an entry for every variable in self.__dict__ which
    is not the optimizer.
    The learning rate lambda functions will only be saved if they are callable objects
    and not if they are functions or lambdas.

    When saving or loading the scheduler, please make sure to also save or load the state of the optimizer.
    """
    state_dict = {key: value for key, value in self.__dict__.items() if key not in ('optimizer', 'lr_lambdas')}
    state_dict['lr_lambdas'] = [None] * len(self.lr_lambdas)

    for idx, fn in enumerate(self.lr_lambdas):
        if not isinstance(fn, types.FunctionType):
            state_dict['lr_lambdas'][idx] = fn.__dict__.copy()

    return state_dict

def load_state_dict(self, state_dict):
    """Loads the schedulers state.

    When saving or loading the scheduler, please make sure to also save or load the state of the optimizer.

    Args:
        state_dict (dict): scheduler state. Should be an object returned
            from a call to :meth:`state_dict`.
    """
    def assign_scalar_to_tensor(data, saved_data):
        if isinstance(saved_data, dict):
            for key, value in saved_data.items():
                if key in data:
                    assign_scalar_to_tensor(data[key], value)  # 递归调用以处理嵌套字典
        elif isinstance(saved_data, list):
            for i, item in enumerate(saved_data):
                assign_scalar_to_tensor(data[i], item)  # 递归调用以处理嵌套列表
        elif isinstance(data, mindspore.Tensor):
            data.assign_value(Tensor(saved_data))  # 将标量值填充到Tensor中

    lr_lambdas = state_dict.pop('lr_lambdas')
    assign_scalar_to_tensor(self.__dict__, state_dict)
    # Restore state_dict keys in order to prevent side effects
    # https://github.com/pytorch/pytorch/issues/32756
    state_dict['lr_lambdas'] = lr_lambdas

    for idx, fn in enumerate(lr_lambdas):
        if fn is not None:
            self.lr_lambdas[idx].__dict__.update(fn)

mindspore.experimental.optim.lr_scheduler.LambdaLR.state_dict = state_dict
mindspore.experimental.optim.lr_scheduler.LambdaLR.load_state_dict = load_state_dict

def _str(self):
    """
    Returns a string representation of the Parameter.
    
    Args:
        self (object): The Parameter object.
        
    Returns:
        str: A string representation of the Parameter object along with the information about 'requires_grad' attribute.
    
    Raises:
        None
    """
    return f'Parameter ({Tensor_.__str__(self)}, ' \
           f'requires_grad={self.requires_grad})'

Parameter.__str__ = _str

class CustomDropout(nn.Cell):

    r"""
    CustomDropout represents a custom dropout layer for neural network models.
    
    This class inherits from nn.Cell and implements a custom dropout function with a dropout probability parameter 'p'. 
    The 'p' parameter controls the probability of setting elements to zero during training. During inference, this layer 
    simply returns the input 'x' without applying dropout. 
    
    Attributes:
        p (float): The probability of setting elements to zero.
    
    Methods:
        construct(x): Applies dropout to the input tensor 'x' based on the dropout probability 'p' during training. 
                      Returns the input tensor 'x' with dropout applied.
    
    Note: The 'ops' module is assumed to be imported for the 'rand_like' function.
    """
    def __init__(self, p=0.5):
        r"""
        Args:
            self (object): The instance of the CustomDropout class.
            p (float): The probability with which elements of the input tensor are set to zero. 
                It should be a float between 0 and 1. Defaults to 0.5.
        
        Returns:
            None: This method does not return any value.
        
        Raises:
            TypeError: If the input value of p is not a float.
            ValueError: If the input value of p is not within the range of 0 to 1.
        """
        super(CustomDropout, self).__init__()
        self.p = p

    def construct(self, x):
        r"""
        Constructs a custom dropout layer for the given input data.
        
        Args:
            self (CustomDropout): An instance of the CustomDropout class.
            x (Tensor): The input data to the dropout layer. It should be a tensor of any shape.
            
        Returns:
            None: This method does not return any value. It modifies the input tensor in-place.
        
        Raises:
            None: This method does not raise any exceptions.
        """
        if not self.training:
            return x
        mask = ops.rand_like(x) > self.p
        return x * mask / (1 - self.p)

nn.Dropout = CustomDropout

def dropout(input, p=0.5, training=True, seed=None):
    """
    Implements the dropout function to apply dropout regularization during training.
    
    Args:
        input (tensor): The input tensor to apply dropout regularization to.
        p (float, optional): The probability of setting elements to zero. Default is 0.5.
        training (bool, optional): If True, applies dropout during training; if False, returns the input as is. Default is True.
        seed (int, optional): The random seed for reproducibility. Default is None.
    
    Returns:
        None: This function modifies the input tensor in-place.
    
    Raises:
        ValueError: If the probability 'p' is not within the range [0, 1].
        TypeError: If the input tensor is not a valid data type.
    """
    if training is False:
        return input
    mask = ops.rand_like(input) > p
    return input * mask / (1 - p)

ops.dropout = dropout

old_cell_call = nn.Cell.__call__
def _cell_call(self, *args, **kwargs):
    r"""
    This function is used to make a call to the '_cell_call' method. 
    
    Args:
        self: The instance of the class.
    
    Returns:
        None. The function does not return any value.
    
    Raises:
        None. The function does not raise any exceptions.
    """
    return self._run_construct(args, kwargs)

nn.Cell.__call__ = _cell_call

def get_cell(self, target):
    r"""
    This function retrieves the specified cell based on the given target.
    
    Args:
        self: The current cell object.
        target (str): The target cell to retrieve. It should be a dot-separated string representing the desired cell's attributes.
    
    Returns:
        nn.Cell: The retrieved cell based on the target.
    
    Raises:
        AttributeError: If the target attribute does not exist in the current cell object.
        AttributeError: If the attribute in the target is not of type nn.Cell.
    """
    if target == "":
        return self

    atoms: List[str] = target.split(".")
    mod: nn.Cell = self

    for item in atoms:

        if not hasattr(mod, item):
            raise AttributeError(mod._get_name() + " has no "
                                "attribute `" + item + "`")

        mod = getattr(mod, item)

        if not isinstance(mod, nn.Cell):
            raise AttributeError("`" + item + "` is not "
                                "an nn.Cell")

    return mod

nn.Cell.get_cell = get_cell

def _set_attr_for_parameter_in_list_or_tuple(self, name, value):
    """Set attr for parameter in list or tuple."""
    for item in value:
        if item in self.exist_objs:
            # If there are multiple identical objects, their names only check once.
            continue
        self.exist_objs.add(item)
        if item.name == PARAMETER_NAME_DEFAULT:
            item.name = item.name + "$" + str(self._id)
            self._id += 1
    object.__setattr__(self, name, value)

nn.Cell._set_attr_for_parameter_in_list_or_tuple = _set_attr_for_parameter_in_list_or_tuple

def _set_attr_for_parameter_tuple(self, name, value):
    """Set attr for parameter in ParameterTuple."""
    params = self.__dict__.get('_params')
    params_list = self.__dict__.get('_params_list')
    if params is None:
        raise AttributeError("For 'Cell', can not assign params before Cell.__init__() is called.")
    exist_objs = set()
    for item in value:
        if item in exist_objs:
            # If there are multiple identical objects, their names only check once.
            continue
        exist_objs.add(item)
        if item.name == PARAMETER_NAME_DEFAULT:
            logger.warning(f"For 'Cell', the parameter definition is deprecated.\n"
                           f"Please set a unique name for the parameter in ParameterTuple '{value}'.")
            item.name = item.name + "$" + str(self._id)
            self._id += 1
        self.insert_param_to_cell(item.name, item, check_name_contain_dot=False)

    if name in self.__dict__:
        del self.__dict__[name]
    if name in params:
        del params[name]
    params_list[name] = value

nn.Cell._set_attr_for_parameter_tuple = _set_attr_for_parameter_tuple

def _check_names(self):
    r"""
    This function does not have any parameters. It is used to perform a check on names and does not return any value.
    
    """

nn.Cell.check_names = _check_names

def requires_grad_(self, requires_grad: bool = True):
    r"""
    Sets the 'requires_grad' attribute of all parameters in the model to the given value.
    
    Args:
        self: The model object.
        requires_grad (bool): Whether to require gradients for the parameters. Defaults to True.
    
    Returns:
        None
    
    Raises:
        None
    """
    for p in self.get_parameters():
        p.requires_grad = requires_grad
    return self

nn.Cell.requires_grad_ = requires_grad_

def __new__(cls, iterable):
    """Create instance object of ParameterTuple."""
    data = tuple(iterable)
    ids = set()
    for x in data:
        if not isinstance(x, Parameter):
            raise TypeError(f"For ParameterTuple initialization, "
                            f"ParameterTuple input should be 'Parameter' collection, "
                            f"but got a {type(iterable)}. ")
        if id(x) not in ids:
            ids.add(id(x))
    return tuple.__new__(ParameterTuple, tuple(data))

ParameterTuple.__new__ = __new__
