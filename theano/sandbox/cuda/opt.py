import logging
_logger = logging.getLogger('theano.sandbox.cuda.opt')

import sys
import theano
import numpy
from theano import scalar as scal
from theano import tensor, compile, gof
from theano.gof import local_optimizer, EquilibriumDB, SequenceDB, Optimizer, toolbox, DestroyHandler, EquilibriumOptimizer

from theano.sandbox.cuda.basic_ops import *
from theano.sandbox.cuda.type import CudaNdarrayType
from theano.sandbox.cuda.blas import (gpu_dot22, gpu_dot22scalar, gpu_gemm_inplace,
        gpu_gemm_no_inplace, GpuConv)
from theano.sandbox.cuda.blas import GpuDownsampleFactorMax, GpuDownsampleFactorMaxGrad
from theano.sandbox.cuda.nnet import (
        GpuCrossentropySoftmaxArgmax1HotWithBias,
        GpuCrossentropySoftmax1HotWithBiasDx,
        GpuSoftmax, GpuSoftmaxWithBias)
from theano.compile import optdb
#optdb.print_summary()  # this shows what is currently registered (in a so-far crude way...)

gpu_optimizer = EquilibriumDB()
gpu_cut_copies = EquilibriumDB()
gpu_seqopt = SequenceDB()
gpu_seqopt.register('gpu_local_optimizations', gpu_optimizer, 1, 'fast_run', 'inplace')
gpu_seqopt.register('gpu_cut_transfers', gpu_cut_copies, 2, 'fast_run', 'inplace')
optdb.register('gpu', gpu_seqopt, optdb.__position__.get('inplace_opt', 75) + 5)

def register_opt(*tags, **kwargs):
    def f(local_opt):
        name = (kwargs and kwargs.pop('name')) or local_opt.__name__
        gpu_optimizer.register(name, local_opt, 'fast_run', 'inplace', *tags)
        return local_opt
    return f

#register local_track_shape_i at this level too to make multi-level lift of shape work.
register_opt()(theano.tensor.opt.local_track_shape_i)

class InputToGpuOptimizer(Optimizer):
    """Transfert the input of a graph to the gpu if needed
    It should make this part of the optimizer faster we will will need only 1 pass on the env.
    """
    def __init__(self):
        Optimizer.__init__(self)

    def add_requirements(self, env):
        env.extend(toolbox.ReplaceValidate())
        env.extend(DestroyHandler())

    def apply(self, env):
        for input in env.inputs:
            if not isinstance(input.type, CudaNdarrayType):
                try:
                    new_input = host_from_gpu(gpu_from_host(input))

                    env.replace_validate(input, new_input, "To allow further optimisation to move Ops to gpu")
                except Exception, e:
                    #as we currently only support float32, this can fail. 
                    #Using try except make that we won't need 
                    pass

#we register it before all other gpu optimizer to be sure that the input are on the gpu.
gpu_seqopt.register('InputToGpuOptimizer', InputToGpuOptimizer(), 
                    0, 'fast_run', 'fast_compile', 'merge')#TODO: how to make it mandatory for gpu_seqopt?

@local_optimizer([])
def local_cut_gpu_host_gpu(node):
    if tensor.opt.opt.check_chain(node, gpu_from_host, host_from_gpu):
        return [node.inputs[0].owner.inputs[0]]
    if tensor.opt.opt.check_chain(node, host_from_gpu, gpu_from_host):
        return [node.inputs[0].owner.inputs[0]]
    return False
gpu_cut_copies.register('cut_gpu_host_transfers', local_cut_gpu_host_gpu, 
        'fast_run', 'inplace', 'gpu')
gpu_cut_copies.register('cut_gpu_constant_transfers', tensor.opt.constant_folding, 
        'fast_run', 'gpu')
#register it into canonicalize to allow other optimization to work without
#botering with this useless pattern.
compile.optdb['canonicalize'].register('local_cut_gpu_host_gpu', local_cut_gpu_host_gpu, 'fast_run')

@register_opt()
@local_optimizer([])
def local_gpu_elemwise_0(node):
    """elemwise(..., host_from_gpu, ...) 
       -> host_from_gpu(elemwise(gpu_from_host, ..., gpu_from_host)
    """
    if isinstance(node.op, tensor.Elemwise):
        if numpy.any([i.owner and isinstance(i.owner.op, HostFromGpu) for i in node.inputs]):
            if numpy.all([o.type.dtype == 'float32' for o in node.outputs]):
                new_op = GpuElemwise(node.op.scalar_op, node.op.inplace_pattern)

                # case 1 - all inputs are already float32
                if numpy.all([i.type.dtype == 'float32' for i in node.inputs]):
                    #TODO: change this when fusion makes Elemwise with multiple outputs
                    return [host_from_gpu(new_op(*(gpu_from_host(i) for i in node.inputs)))]

                # THIS IS PROBABLY TRUE....
                # case 2 - it would still be ok if some inputs were upcast to float32
                #   first establish that float32 can store all inputs
                upcastable = set(['float32', 'int8', 'int16', 'uint8', 'uint16'])
                if numpy.all([i.type.dtype in upcastable for i in node.inputs]):
                    # second - establish that a new node with upcasted inputs has the same outputs
                    # types as the original node
                    casted = node.op.make_node(*[tensor.cast(i, 'float32') for i in node.inputs])
                    if [o.type for o in casted.outputs] == [o.type for o in node.outputs]:

                        new_inputs = [gpu_from_host(tensor.cast(i, 'float32')) for i in node.inputs]

                        return [host_from_gpu(new_op(*new_inputs))]

@register_opt()
@local_optimizer([])
def local_gpu_elemwise_1(node):
    """
    gpu_from_host(Elemwise)) -> GpuElemwise(gpu_from_host(...))
    """
    if node.op == gpu_from_host:
        host_i, = node.inputs
        if host_i.owner and isinstance(host_i.owner.op, tensor.Elemwise) and len(host_i.clients)==1:
            elemwise_node = host_i.owner
            new_op = GpuElemwise(elemwise_node.op.scalar_op, elemwise_node.op.inplace_pattern)
            return [new_op(*(gpu_from_host(i) for i in elemwise_node.inputs))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_dimshuffle_0(node):
    """
    dimshuffle(host_from_gpu()) -> host_from_gpu(gpu_dimshuffle)
    gpu_from_host(dimshuffle) -> gpu_dimshuffle(gpu_from_host)
    """
    if isinstance(node.op, tensor.DimShuffle):
        input, = node.inputs
        if input.owner and isinstance(input.owner.op, HostFromGpu):
            # move the add to a GpuAdd
            new_op = GpuDimShuffle(node.op.input_broadcastable, 
                    node.op.new_order)
            if node.op.inplace:
                return [host_from_gpu(new_op(gpu_from_host(input)))]
            else:
                return [host_from_gpu(new_op(gpu_from_host(tensor.tensor_copy(input))))]
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and isinstance(host_input.owner.op, tensor.DimShuffle):
            dimshuffle_node = host_input.owner
            new_op = GpuDimShuffle(dimshuffle_node.op.input_broadcastable, 
                    dimshuffle_node.op.new_order)
            return [new_op(gpu_from_host(dimshuffle_node.inputs[0]))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_dot22(node):
    """
    gpu_from_host(dot22) -> gpudot(gpu_from_host)

    dot(host_from_gpu) -> host_from_gpu(gpudot22)
    """
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and host_input.owner.op == tensor.blas._dot22:
            x, y = host_input.owner.inputs
            return [gpu_dot22(gpu_from_host(x), gpu_from_host(y))]
    if node.op == tensor.blas._dot22:
        if numpy.any([(i.owner and i.owner.op == host_from_gpu) for i in node.inputs]):
            x, y = node.inputs
            return [host_from_gpu(gpu_dot22(gpu_from_host(x), gpu_from_host(y)))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_dot22scalar(node):
    """
    gpu_from_host(dot22scalar) -> gpudot(gpu_from_host)

    dot(host_from_gpu) -> host_from_gpu(gpudot22scalar)
    """
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and host_input.owner.op == tensor.blas._dot22scalar:
            x, y, scalar = host_input.owner.inputs
            return [gpu_dot22scalar(gpu_from_host(x), gpu_from_host(y), tensor.blas._as_scalar(scalar))]
    if node.op == tensor.blas._dot22scalar:
        if numpy.any([(i.owner and i.owner.op == host_from_gpu) for i in node.inputs]):
            x, y, scalar = node.inputs
            return [host_from_gpu(gpu_dot22scalar(gpu_from_host(x), gpu_from_host(y),tensor.blas._as_scalar(scalar)))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_gemm(node):
    """
    gpu_from_host(gemm) -> gpu_gemm(gpu_from_host)

    gemm(host_from_gpu) -> host_from_gpu(gpu_gemm)
    """
    gemms = {tensor.blas.gemm_inplace: gpu_gemm_inplace,
            tensor.blas.gemm_no_inplace: gpu_gemm_no_inplace}
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and host_input.owner.op in gemms:
            op = host_input.owner.op
            z, a, x, y, b = host_input.owner.inputs
            return [gemms[op](gpu_from_host(z), a, gpu_from_host(x), gpu_from_host(y), b)]
    if node.op in gemms:
        z, a, x, y, b = node.inputs
        x_on_gpu = (x.owner and x.owner.op == host_from_gpu)
        y_on_gpu = (y.owner and y.owner.op == host_from_gpu)
        z_on_gpu = (z.owner and z.owner.op == host_from_gpu)
        if x_on_gpu or y_on_gpu or z_on_gpu:
            return [host_from_gpu(gemms[node.op](gpu_from_host(z), a, gpu_from_host(x), gpu_from_host(y), b))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_sum(node):
    if isinstance(node.op, tensor.elemwise.CAReduce):
        if node.op.scalar_op == scal.add:
            x, = node.inputs
            if x.owner and x.owner.op == host_from_gpu:
                if node.op.axis is None:
                    reduce_mask = [1] * x.type.ndim
                else:
                    reduce_mask = [0] * x.type.ndim
                    for a in node.op.axis:
                        assert reduce_mask[a] == 0
                        reduce_mask[a] = 1
                gsum=GpuSum(reduce_mask)
                pattern=(''.join(str(i) for i in reduce_mask))
                if hasattr(gsum, 'c_code_reduce_%s'%pattern):
                    rval = host_from_gpu(gsum(gpu_from_host(x)))
                    if rval.type == node.outputs[0].type:
                        return [rval]
                    else:
                        print >> sys.stderr, "WARNING: local_gpu_sum got type wrong"
                        return None
                else:

                    # Try to make a simpler pattern based on reshaping
                    # The principle is that if two adjacent dimensions have the same value in
                    # the reduce_mask, then we can reshape to make them a single dimension, do
                    # the sum, and then reshape to get them back.

                    shape_of = node.env.shape_feature.shape_of

                    x_shape = shape_of[x]

                    new_in_shp = [x_shape[0]]
                    new_mask = [reduce_mask[0]]
                    for i in range(1, x.type.ndim):
                        if reduce_mask[i] == reduce_mask[i-1]:
                            new_in_shp[-1] *= x_shape[i]
                        else:
                            new_mask.append(reduce_mask[i])
                            new_in_shp.append(x_shape[i])

                    pattern=(''.join(str(i) for i in new_mask))
                    new_gsum = GpuSum(new_mask)
                    if hasattr(new_gsum, 'c_code_reduce_%s'%pattern):
                        reshaped_x = x.reshape(tensor.stack(*new_in_shp))
                        sum_reshaped_x = host_from_gpu(new_gsum(gpu_from_host(reshaped_x)))
                        unreshaped_sum = sum_reshaped_x.reshape(tensor.stack(*shape_of[node.outputs[0]]))
                        if unreshaped_sum.type == node.outputs[0].type:
                            return [unreshaped_sum]
                        else:
                            print >> sys.stderr, "WARNING: local_gpu_sum got type wrong"
                            return None

                        raise Exception("GpuSum don't have implemented the pattern",pattern)
    return False

@register_opt()
@local_optimizer([])
def local_gpu_reshape(node):
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and isinstance(host_input.owner.op, tensor.Reshape):
            rshp = host_input.owner.op
            x, shp = host_input.owner.inputs
            return [GpuReshape(rshp.ndim)(gpu_from_host(x), shp)]
    if isinstance(node.op, tensor.Reshape):
        x, shp = node.inputs
        if x.owner and x.owner.op == host_from_gpu:
            gpu_x, = x.owner.inputs
            return [host_from_gpu(GpuReshape(node.op.ndim)(gpu_x, shp))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_flatten(node):
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and isinstance(host_input.owner.op, tensor.Flatten):
            outdim = host_input.owner.op.outdim
            return [GpuFlatten(outdim)(gpu_from_host(host_input.owner.inputs[0]))]
    if isinstance(node.op, tensor.Flatten):
        x, = node.inputs
        outdim = node.op.outdim
        if x.owner and x.owner.op == host_from_gpu:
            gpu_x, = x.owner.inputs
            return [host_from_gpu(GpuFlatten(outdim)(gpu_x))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_subtensor(node):
    if node.op == gpu_from_host:
        host_input = node.inputs[0]
        if host_input.owner and isinstance(host_input.owner.op, tensor.Subtensor):
            subt = host_input.owner.op
            x = host_input.owner.inputs[0]
            coords = host_input.owner.inputs[1:]
            return [GpuSubtensor(subt.idx_list)(gpu_from_host(x), *coords)]
    if isinstance(node.op, tensor.Subtensor):
        x  = node.inputs[0]
        coords = node.inputs[1:]
        if x.owner and x.owner.op == host_from_gpu:
            gpu_x, = x.owner.inputs
            return [host_from_gpu(GpuSubtensor(node.op.idx_list)(gpu_x, *coords))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_incsubtensor(node):
    if node.op == gpu_from_host:
        host_output = node.inputs[0]
        if host_output.owner and type(host_output.owner.op) == tensor.IncSubtensor:
            incsubt = host_output.owner.op
            x, y = host_output.owner.inputs[0:2]
            coords = host_output.owner.inputs[2:]
            return [GpuIncSubtensor(incsubt.idx_list, inplace=incsubt.inplace)(
                gpu_from_host(x),
                gpu_from_host(y),
                *coords)]
    if type(node.op) == tensor.IncSubtensor:
        x, y = node.inputs[0:2]
        assert isinstance(x.type, tensor.TensorType)
        assert isinstance(y.type, tensor.TensorType)
        coords = node.inputs[2:]
        go_gpu = False
        if x.owner and x.owner.op == host_from_gpu:
            go_gpu = True
            gpu_x, = x.owner.inputs
        else:
            gpu_x = gpu_from_host(x)
        if y.owner and y.owner.op == host_from_gpu:
            go_gpu = True
            gpu_y, = y.owner.inputs
        else:
            gpu_y = gpu_from_host(y)
        if go_gpu:
            return [host_from_gpu(GpuIncSubtensor(
                node.op.idx_list, inplace=node.op.inplace)(
                    gpu_x, gpu_y, *coords))]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_shape(node):
    if isinstance(node.op, tensor.Shape):
        x, = node.inputs
        if x.owner and x.owner.op == host_from_gpu:
            gpu_x, = x.owner.inputs
            return [gpu_shape(gpu_x)]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_rebroadcast(node):
    '''rebroadcast(host_from_gpu(x)) -> host_from_gpu(rebroadcast(x))'''
    if isinstance(node.op, tensor.Rebroadcast):
        x, = node.inputs
        if (x.owner and x.owner.op == host_from_gpu):
            gpu_x = x.owner.inputs[0]
            return [host_from_gpu(node.op(gpu_x))]

def cast(x, dtype):
    stype = scal.Scalar(dtype)
    cast_op = theano.tensor.Elemwise(scal.Identity(scal.specific_out(stype)))
    return cast_op(x)

import theano.tensor.nnet
@register_opt()
@local_optimizer([])
def local_gpu_crossentorpy_softmax_argmax_1hot_with_bias(node):
    if isinstance(node.op, tensor.nnet.CrossentropySoftmaxArgmax1HotWithBias):
        x,b,y = node.inputs
        if x.owner and x.owner.op == host_from_gpu:
            gpu_x, = x.owner.inputs
            # if y is a cast to integers, we can go to the underlying thing if we want, 
            # since this gpu op will cast to integers internally anyway
            int_cast_ops = (
                    tensor.basic._convert_to_int32,
                    tensor.basic._convert_to_int8,
                    tensor.basic._convert_to_int16,
                    tensor.basic._convert_to_int64,
                    )
            while y.owner and y.owner.op in int_cast_ops:
                y = y.owner.inputs[0]
            gpu_nll, gpu_sm, gpu_am = GpuCrossentropySoftmaxArgmax1HotWithBias()(
                gpu_x,
                gpu_from_host(b),
                gpu_from_host(cast(y, 'float32')))
            am_dtype = node.outputs[2].type.dtype
            return [host_from_gpu(gpu_nll), 
                    host_from_gpu(gpu_sm), 
                    cast(host_from_gpu(gpu_am), am_dtype)]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_crossentorpy_softmax_1hot_with_bias_dx(node):
    if isinstance(node.op, tensor.nnet.CrossentropySoftmax1HotWithBiasDx):
        dnll,sm,yidx = node.inputs
        if sm.owner and sm.owner.op == host_from_gpu:
            gpu_sm, = sm.owner.inputs
            gpu_dx = GpuCrossentropySoftmax1HotWithBiasDx()(
                gpu_from_host(dnll),
                gpu_sm,
                gpu_from_host(cast(yidx, 'float32')))
            return [host_from_gpu(gpu_dx)]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_softmax(node):
    if isinstance(node.op, tensor.nnet.Softmax):
        x, = node.inputs
        if x.owner and x.owner.op == host_from_gpu:
            gpu_x, = x.owner.inputs
            gpu_sm = GpuSoftmax()(gpu_x)
            return [host_from_gpu(gpu_sm)]
    return False

@register_opt()
@local_optimizer([])
def local_gpu_softmax_with_bias(node):
    if isinstance(node.op, tensor.nnet.SoftmaxWithBias):
        x, b = node.inputs
        x_on_gpu = x.owner and x.owner.op == host_from_gpu
        b_on_gpu = b.owner and b.owner.op == host_from_gpu
        if x_on_gpu or b_on_gpu:
            gpu_sm = GpuSoftmaxWithBias()(gpu_from_host(x), gpu_from_host(b))
            return [host_from_gpu(gpu_sm)]
    return False

#### Convolution, maxpooling
from theano.tensor.nnet import conv
@register_opt()
@local_optimizer([])
def local_gpu_conv(node):
    """
    gpu_from_host(conv) -> gpu_conv(gpu_from_host)

    conv(host_from_gpu) -> host_from_gpu(gpu_conv)
    """
    def GpuConvOp_from_ConvOp(op):
        logical_img_hw=None
        if op.imshp_logical is not None:
            logical_img_hw=op.imshp_logical[1:3]
        ret = GpuConv(border_mode=op.out_mode,
                    subsample=(op.dx, op.dy),
                    logical_img_hw=logical_img_hw,
                    logical_kern_hw=op.kshp_logical,
                    logical_kern_align_top=op.kshp_logical_top_aligned,
                    kshp=op.kshp,
                    version=op.version,
                    verbose=op.verbose
                    )
        #HACK to print the number of MFlops in the profiler output.
        if hasattr(op,'flops'):
            ret.flops=op.flops
        return ret

    if node.op == gpu_from_host:
        #gpu_from_host(conv) -> gpu_conv(gpu_from_host)
        host_input = node.inputs[0]
        if host_input.owner and isinstance(host_input.owner.op, conv.ConvOp):
            gpu_conv = GpuConvOp_from_ConvOp(host_input.owner.op)
            img, kern = host_input.owner.inputs
            return [gpu_conv(gpu_from_host(img), gpu_from_host(kern))]

    if isinstance(node.op, conv.ConvOp):
        #conv(host_from_gpu) -> host_from_gpu(gpu_conv)
        img, kern = node.inputs
        img_on_gpu = (img.owner and img.owner.op == host_from_gpu)
        kern_on_gpu = (kern.owner and kern.owner.op == host_from_gpu)
        if img_on_gpu or kern_on_gpu:
            gpu_conv = GpuConvOp_from_ConvOp(node.op)
            return [host_from_gpu(gpu_conv(gpu_from_host(img), gpu_from_host(kern)))]

import theano.tensor.signal.downsample as downsample
@register_opt()
@local_optimizer([])
def local_gpu_downsample_factor_max(node):
    if isinstance(node.op, downsample.DownsampleFactorMax):
        x, = node.inputs
        if (x.owner and x.owner.op == host_from_gpu):
            gpu_ds = GpuDownsampleFactorMax(node.op.ds, node.op.ignore_border)
            return [host_from_gpu(gpu_ds(x.owner.inputs[0]))]

@register_opt()
@local_optimizer([])
def local_gpu_downsample_factor_max_grad(node):
    if isinstance(node.op, downsample.DownsampleFactorMaxGrad):
        x,z,gz = node.inputs
        if (x.owner and x.owner.op == host_from_gpu):
            gpu_ds_grad = GpuDownsampleFactorMaxGrad(node.op.ds, node.op.ignore_border)
            return [host_from_gpu(gpu_ds_grad(x.owner.inputs[0], gpu_from_host(z), gpu_from_host(gz)))]



from theano.sandbox.cuda.basic_ops import gpu_join

@register_opt()
@local_optimizer([])
def local_gpu_join(node):
    """
    Inspired by the opt for convop.

    Very loose notation follows.

    Subgraphs concerned first look like
        [array of HostTensor] -> HostToGpu -> GpuToHost
        -> Join -> HostToGpu -> GpuToHost

    First we apply this Opt:

    join(host_from_gpu) -> host_from_gpu(gpu_join)

    then, as an intermediate result, there should be
    host_from_gpu(gpu_join) -> HostToGpu -> GpuToHost
    this unnecessary GpuToHost -> HostToGpu should be removed
    by other opts, leaving us with
    host_from_gpu(gpu_join)


    For intermediate places in the graph not covered by the first opt, the following could be useful:

    gpu_from_host(join) -> gpu_join(gpu_from_host)

    not implemented yet.

    """
    if isinstance(node.op, tensor.Join):
        # optimizing this case:
        # join(host_from_gpu) -> host_from_gpu(gpu_join)

        # print "OPT: we've got a Join instance"

        axis_and_tensors = node.inputs

        #print "OPT: axis_and_tensors=", axis_and_tensors

        matches = [not t.owner is None and t.owner.op == host_from_gpu for t in axis_and_tensors[1:]]

        #print "OPT: matches =", matches

        # if all input tensors are host_from_gpu'ified
        if numpy.all(matches):
            # the extra gpu_from_host introduced here will
            # be removed by further optimizations
            new_tensors = [gpu_from_host(t) for t in axis_and_tensors[1:]]
            new_a_and_t = [axis_and_tensors[0]]+new_tensors

            replacement_node = host_from_gpu(gpu_join(*new_a_and_t))

            # print "OPT: replacement_node", replacement_node

            return [replacement_node]


@local_optimizer([gpu_gemm_no_inplace])
def local_inplace_gemm(node):
    if node.op == gpu_gemm_no_inplace:
        return [gpu_gemm_inplace(*node.inputs)]

# After destroyhandler is in but before we try to make elemwise things inplace
# Try to make gpu gemm inplace
# Also, need to make the gemm optimisation(step 70) happen before the fusion of elemwise(step 71)
optdb.register('InplaceGpuBlasOpt',
        EquilibriumOptimizer([local_inplace_gemm], failure_callback=EquilibriumOptimizer.warn_inplace,
            max_use_ratio=5),
               70.0, 'fast_run', 'inplace')

#GpuElemwise fusion
gpu_local_elemwise_fusion = tensor.opt.local_elemwise_fusion_op(GpuElemwise)
if config.gpu.local_elemwise_fusion:
    _logger.debug("enabling optimization fusion of gpu elemwise in fast_run")
    compile.optdb.register('gpu_elemwise_fusion', tensor.opt.FusionOptimizer(gpu_local_elemwise_fusion), 71.00, 'fast_run', 'fusion', 'local_elemwise_fusion')
else:
    _logger.debug("not enabling optimization fusion of gpu elemwise in fast_run")
    compile.optdb.register('gpu_elemwise_fusion', tensor.opt.FusionOptimizer(gpu_local_elemwise_fusion), 71.00, 'fusion', 'local_elemwise_fusion')


@register_opt()
@local_optimizer([tensor.Alloc])
def local_gpualloc(node):
    if node.op == tensor.alloc:
        if node.inputs[0].owner and node.inputs[0].owner.op==host_from_gpu:#if the input was on the gpu
            new_node = host_from_gpu(gpu_alloc(*node.inputs))
            return [new_node]
        if all([c!='output' and c.op == gpu_from_host for c,idx in node.outputs[0].clients]):#if all clients are on gpu
            new_node = host_from_gpu(gpu_alloc(*node.inputs))
            return [new_node]
        if all([c!='output' and c.op == tensor.join and all([i.owner and i.owner.op in [host_from_gpu,tensor.alloc] for i in c.inputs[1:]]) for c,idx in node.outputs[0].clients]):#if the client is a subtensor with input on gpu or alloc
            new_node = host_from_gpu(gpu_alloc(*node.inputs))
            return [new_node]
            
