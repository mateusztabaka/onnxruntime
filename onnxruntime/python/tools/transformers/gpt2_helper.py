# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.  See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
# This script helps onnx conversion and validation for GPT2 model with past state.
import os
import logging
import torch
import onnx
import random
import numpy
import time
import re
from pathlib import Path
from typing import List, Dict, Tuple, Union
from transformers import GPT2Model, GPT2LMHeadModel, GPT2Config, TFGPT2Model
from benchmark_helper import Precision

logger = logging.getLogger(__name__)

PRETRAINED_GPT2_MODELS = ['distilgpt2', 'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl']

DEFAULT_TOLERANCE = {Precision.FLOAT32: 0.0005, Precision.FLOAT16: 0.2, Precision.INT8: 3.0}


class GPT2ModelNoPastState(GPT2Model):
    """ Here we wrap a class to disable past state output.
    """
    def __init__(self, config):
        super().__init__(config)

    def forward(self, input_ids):
        return super().forward(input_ids, use_cache=False, return_dict=False)


class TFGPT2ModelNoPastState(TFGPT2Model):
    """ Here we wrap a class to disable past state output.
    """
    def __init__(self, config):
        config.use_cache = False
        super().__init__(config)

    def forward(self, input_ids):
        return super().call(input_ids, use_cache=False)


class MyGPT2Model(GPT2Model):
    """ Here we wrap a class for Onnx model conversion for GPT2Model with past state.
    """
    def __init__(self, config):
        super().__init__(config)

    @staticmethod
    def post_process(result, num_layer):
        if isinstance(result[1][0], tuple) or isinstance(result[1][0], list):
            assert len(result[1]) == num_layer and len(result[1][0]) == 2
            #assert len(result[1][0][0].shape) == 4 and result[1][0][0].shape == result[1][0][1].shape
            present = []
            for i in range(num_layer):
                # Since transformers v4.*, past key and values are separated outputs.
                # Here we concate them into one tensor to be compatible with Attention operator.
                present.append(torch.cat((result[1][i][0].unsqueeze(0), result[1][i][1].unsqueeze(0)), dim=0))
            return (result[0], tuple(present))

        return result

    def forward(self, input_ids, position_ids, attention_mask, *past):
        result = super().forward(input_ids,
                                 position_ids=position_ids,
                                 attention_mask=attention_mask,
                                 past_key_values=past,
                                 return_dict=False)
        return MyGPT2Model.post_process(result, self.config.n_layer)


class MyGPT2LMHeadModel(GPT2LMHeadModel):
    """ Here we wrap a class for Onnx model conversion for GPT2LMHeadModel with past state.
    """
    def __init__(self, config):
        super().__init__(config)

    def forward(self, input_ids, position_ids, attention_mask, *past):
        result = super().forward(input_ids,
                                 position_ids=position_ids,
                                 attention_mask=attention_mask,
                                 past_key_values=past,
                                 return_dict=False)

        return MyGPT2Model.post_process(result, self.config.n_layer)


class MyGPT2LMHeadModel_NoPadding(GPT2LMHeadModel):
    """ Here we wrap a class for Onnx model conversion for GPT2LMHeadModel with past state and no padding.
        When you always use batch_size=1 in inference, there is no padding in inputs. In such case, position_ids
        and attention_mask need no be in inputs.
    """
    def __init__(self, config):
        super().__init__(config)

    def forward(self, input_ids, *past):
        return super().forward(input_ids, past_key_values=past)


# Maps model class name to a tuple of model class, name of first output and use padding or not
MODEL_CLASSES = {
    'GPT2LMHeadModel': (MyGPT2LMHeadModel, 'logits', True),
    'GPT2LMHeadModel_NoPadding': (MyGPT2LMHeadModel_NoPadding, 'logits', False),
    'GPT2Model': (MyGPT2Model, 'last_state', True),
}


class Gpt2Inputs:
    def __init__(self, input_ids, position_ids, attention_mask, past):
        self.input_ids: torch.LongTensor = input_ids
        self.position_ids: torch.LongTensor = position_ids
        self.attention_mask: Union[torch.FloatTensor, torch.HalfTensor] = attention_mask
        self.past: Union[List[torch.FloatTensor], List[torch.HalfTensor]] = past

    def to_list(self) -> List:
        input_list = [v for v in [self.input_ids, self.position_ids, self.attention_mask] if v is not None]
        if self.past:
            input_list.extend(self.past)

        return input_list

    def to_tuple(self) -> Tuple:
        return tuple(v for v in [self.input_ids, self.position_ids, self.attention_mask, self.past] if v is not None)

    def to_fp32(self):
        attention_mask = self.attention_mask.to(dtype=torch.float32) if self.attention_mask is not None else None
        past = [p.to(dtype=torch.float32) for p in self.past]
        return Gpt2Inputs(self.input_ids, self.position_ids, attention_mask, past)


class Gpt2Helper:
    """ A helper class for Gpt2 model conversion, inference and verification.
    """
    @staticmethod
    def get_dummy_inputs(batch_size: int,
                         past_sequence_length: int,
                         sequence_length: int,
                         num_attention_heads: int,
                         hidden_size: int,
                         num_layer: int,
                         vocab_size: int,
                         device: torch.device,
                         float16: bool = False,
                         has_position_ids: bool = True,
                         has_attention_mask: bool = True) -> Gpt2Inputs:
        """ Create random inputs for GPT2 model.
        Returns torch tensors of input_ids, position_ids, attention_mask and a list of past state tensors.
        """
        float_type = torch.float16 if float16 else torch.float32
        past_shape = [2, batch_size, num_attention_heads, past_sequence_length, int(hidden_size / num_attention_heads)]

        past = [torch.rand(past_shape, dtype=float_type, device=device) for _ in range(num_layer)]
        input_ids = torch.randint(low=0,
                                  high=vocab_size - 1,
                                  size=(batch_size, sequence_length),
                                  dtype=torch.int64,
                                  device=device)

        attention_mask = None
        if has_attention_mask:
            total_sequence_length = past_sequence_length + sequence_length
            attention_mask = torch.ones([batch_size, total_sequence_length], dtype=float_type, device=device)
            if total_sequence_length >= 2:
                padding_position = random.randint(0, total_sequence_length - 1)  # test input with padding.
                attention_mask[:, padding_position] = 0

        # Deduce position_ids from attention mask
        position_ids = None
        if has_position_ids:
            position_ids = (attention_mask.long().cumsum(-1) - 1)
            position_ids.masked_fill_(position_ids < 0, 0)
            position_ids = position_ids[:, past_sequence_length:]

        return Gpt2Inputs(input_ids, position_ids, attention_mask, past)

    @staticmethod
    def get_output_shapes(batch_size: int,
                          past_sequence_length: int,
                          sequence_length: int,
                          config: GPT2Config,
                          model_class: str = "GPT2LMHeadModel") -> Dict[str, List[int]]:
        """ Returns a dictionary with output name as key, and shape as value.
        """
        num_attention_heads = config.num_attention_heads
        hidden_size = config.hidden_size
        num_layer = config.num_hidden_layers
        vocab_size = config.vocab_size

        output_name = MODEL_CLASSES[model_class][1]

        last_state_shape = [batch_size, sequence_length, vocab_size if output_name == "logits" else hidden_size]
        present_state_shape = [
            2, batch_size, num_attention_heads, past_sequence_length + sequence_length,
            int(hidden_size / num_attention_heads)
        ]

        output_shapes = {output_name: last_state_shape}
        for i in range(num_layer):
            output_shapes["present_" + str(i)] = present_state_shape

        return output_shapes

    @staticmethod
    def auto_increase_buffer_size(output_buffers, output_shapes):
        for key in output_shapes:
            assert key in output_buffers
            buffer = output_buffers[key]
            if numpy.prod(output_shapes[key]) > buffer.nelement():
                output_buffers[key] = torch.empty(numpy.prod(output_shapes[key]),
                                                  dtype=buffer.dtype,
                                                  device=buffer.device)

    @staticmethod
    def get_output_buffers(output_shapes, device, is_float16=False):
        """ Returns a dictionary of output name as key, and 1D tensor as value. The tensor has enough space for given shape.
        """
        data_type = torch.float16 if is_float16 else torch.float32

        output_buffers = {}
        for name, shape in output_shapes.items():
            output_buffers[name] = torch.empty(numpy.prod(shape), dtype=data_type, device=device)
        return output_buffers

    @staticmethod
    def diff_outputs(torch_outputs, ort_outputs, relative=False):
        """ Returns the maximum difference between PyTorch and OnnxRuntime outputs.
        """
        expected_outputs = torch_outputs[0].cpu().numpy()
        diff = numpy.abs(expected_outputs - ort_outputs[0])
        if relative:
            return numpy.amax(diff / (numpy.abs(expected_outputs) + 1e-6))
        else:
            return numpy.amax(diff)

    @staticmethod
    def compare_outputs(torch_outputs, ort_outputs, rtol=1e-03, atol=1e-03):
        """ Returns True if torch and ORT outputs are close for given thresholds, and False otherwise.
        """
        is_close = numpy.allclose(ort_outputs[0], torch_outputs[0].cpu().numpy(), rtol=rtol, atol=atol)
        logger.debug(f'PyTorch and OnnxRuntime output 0 (last_state) are close: {is_close}')

        is_all_close = is_close
        num_layers = len(ort_outputs) - 1

        for layer in range(num_layers):
            is_close = numpy.allclose(ort_outputs[1 + layer],
                                      torch_outputs[1][layer].cpu().numpy(),
                                      rtol=rtol,
                                      atol=atol)
            logger.debug(f'PyTorch and OnnxRuntime layer {layer} state (present_{layer}) are close:{is_close}')
            is_all_close = is_all_close and is_close

        if not is_all_close:
            max_abs_diff = Gpt2Helper.diff_outputs(torch_outputs, ort_outputs)
            logger.info(f'PyTorch and OnnxRuntime results are not all close: max_abs_diff={max_abs_diff:.5f}')

        return is_all_close

    @staticmethod
    def export_onnx(model,
                    device,
                    onnx_model_path: str,
                    verbose: bool = False,
                    use_external_data_format: bool = False,
                    has_position_ids: bool = True,
                    has_attention_mask: bool = True):
        """ Export GPT-2 model with past state to ONNX model.
        """
        config: GPT2Config = model.config
        num_layer = config.n_layer
        dummy_inputs = Gpt2Helper.get_dummy_inputs(batch_size=1,
                                                   past_sequence_length=1,
                                                   sequence_length=1,
                                                   num_attention_heads=config.num_attention_heads,
                                                   hidden_size=config.hidden_size,
                                                   num_layer=num_layer,
                                                   vocab_size=config.vocab_size,
                                                   device=device,
                                                   float16=False,
                                                   has_position_ids=has_position_ids,
                                                   has_attention_mask=has_attention_mask)
        input_list = dummy_inputs.to_list()

        with torch.no_grad():
            outputs = model(*input_list)

        past_names = [f'past_{i}' for i in range(num_layer)]
        present_names = [f'present_{i}' for i in range(num_layer)]

        # GPT2Model outputs last_state; GPT2LMHeadModel outputs logits (prediction_scores)
        assert outputs[0].shape[2] == config.vocab_size or outputs[0].shape[2] == config.hidden_size
        output_names = ["logits" if outputs[0].shape[2] == config.vocab_size else "last_state"] + present_names

        # Shape of input tensors:
        #    input_ids: (batch_size, seq_len)
        #    past_{i}:  (2, batch_size, num_heads, past_seq_len, hidden_size/num_heads)
        #    attention_mask: (batch_size, past_seq_len + seq_len)
        # Shape of output tensors:
        #    last_state: (batch_size, seq_len, hidden_size)
        #      or logits: (batch_size, seq_len, vocab_size)
        #    present_{i}:  (2, batch_size, num_heads, past_seq_len + seq_len, hidden_size/num_heads)
        dynamic_axes = {'input_ids': {0: 'batch_size', 1: 'seq_len'}, output_names[0]: {0: 'batch_size', 1: 'seq_len'}}
        for name in past_names:
            dynamic_axes[name] = {1: 'batch_size', 3: 'past_seq_len'}
        for name in present_names:
            dynamic_axes[name] = {1: 'batch_size', 3: 'total_seq_len'}

        input_names = ['input_ids']
        if has_position_ids:
            dynamic_axes['position_ids'] = {0: 'batch_size', 1: 'seq_len'}
            input_names.append('position_ids')
        if has_attention_mask:
            dynamic_axes['attention_mask'] = {0: 'batch_size', 1: 'total_seq_len'}
            input_names.append('attention_mask')
        input_names.extend(past_names)

        assert len(outputs) == 2 and len(outputs[1]) == num_layer

        logger.info(
            f"Shapes: input_ids={dummy_inputs.input_ids.shape} past={dummy_inputs.past[0].shape} output={outputs[0].shape} present={outputs[1][0].shape}"
        )

        Path(onnx_model_path).parent.mkdir(parents=True, exist_ok=True)

        torch.onnx.export(model,
                          args=tuple(input_list),
                          f=onnx_model_path,
                          input_names=input_names,
                          output_names=output_names,
                          example_outputs=outputs,
                          dynamic_axes=dynamic_axes,
                          opset_version=11,
                          do_constant_folding=True,
                          use_external_data_format=use_external_data_format,
                          verbose=verbose)

    @staticmethod
    def optimize_onnx(onnx_model_path,
                      optimized_model_path,
                      is_float16,
                      num_attention_heads,
                      hidden_size,
                      use_external_data_format=False):
        """ Optimize ONNX model with an option to convert it to use mixed precision.
        """
        from optimizer import optimize_model
        m = optimize_model(onnx_model_path,
                           model_type='gpt2',
                           num_heads=num_attention_heads,
                           hidden_size=hidden_size,
                           opt_level=0,
                           optimization_options=None,
                           use_gpu=False)
        if is_float16:
            m.convert_model_float32_to_float16(cast_input_output=False)

        m.save_model_to_file(optimized_model_path, use_external_data_format)

    @staticmethod
    def pytorch_inference(model, inputs: Gpt2Inputs, total_runs: int = 0):
        """ Run inference of PyTorch model, and returns average latency in ms when total_runs > 0 besides outputs.
        """
        logger.debug("start pytorch_inference")

        # Convert it to fp32 as the PyTroch model cannot deal with half input.
        input_list = inputs.to_fp32().to_list()

        with torch.no_grad():
            outputs = model(*input_list)

        if total_runs == 0:
            return outputs

        latency = []
        with torch.no_grad():
            for _ in range(total_runs):
                start = time.time()
                outputs = model(*input_list)
                latency.append(time.time() - start)

        average_latency = sum(latency) * 1000 / len(latency)
        logger.debug("PyTorch inference time = {} ms".format(format(average_latency, '.2f')))

        return outputs, average_latency

    @staticmethod
    def onnxruntime_inference(ort_session, inputs: Gpt2Inputs, total_runs: int = 0):
        """ Run inference of ONNX model, and returns average latency in ms when total_runs > 0 besides outputs.
        """
        logger.debug(f"start onnxruntime_inference")

        ort_inputs = {'input_ids': numpy.ascontiguousarray(inputs.input_ids.cpu().numpy())}

        if inputs.past is not None:
            for i, past_i in enumerate(inputs.past):
                ort_inputs[f'past_{i}'] = numpy.ascontiguousarray(past_i.cpu().numpy())

        if inputs.attention_mask is not None:
            ort_inputs['attention_mask'] = numpy.ascontiguousarray(inputs.attention_mask.cpu().numpy())

        if inputs.position_ids is not None:
            ort_inputs['position_ids'] = numpy.ascontiguousarray(inputs.position_ids.cpu().numpy())

        ort_outputs = ort_session.run(None, ort_inputs)
        if total_runs == 0:
            return ort_outputs

        latency = []
        for _ in range(total_runs):
            start = time.time()
            ort_outputs = ort_session.run(None, ort_inputs)
            latency.append(time.time() - start)

        average_latency = sum(latency) * 1000 / len(latency)
        logger.debug("OnnxRuntime Inference time = {} ms".format(format(average_latency, '.2f')))

        return ort_outputs, average_latency

    @staticmethod
    def prepare_io_binding(ort_session, input_ids, position_ids, attention_mask, past, output_buffers, output_shapes):
        """ Returnas IO binding object for a session.
        """

        # Bind inputs and outputs to onnxruntime session
        io_binding = ort_session.io_binding()

        # Bind inputs
        assert input_ids.is_contiguous()
        io_binding.bind_input('input_ids', input_ids.device.type, 0, numpy.longlong, list(input_ids.size()),
                              input_ids.data_ptr())

        data_type = output_buffers[ort_session.get_outputs()[0].name].dtype
        float_type = numpy.float16 if data_type == torch.float16 else numpy.float32

        if past is not None:
            for i, past_i in enumerate(past):
                assert past_i.is_contiguous()

                data_ptr = past_i.data_ptr()
                if data_ptr == 0:
                    # When past_sequence_length is 0, its data_ptr will be zero. IO Binding asserts that data_ptr shall not be zero.
                    # Here we workaround and pass data pointer of input_ids. Actual data is not used for past so it does not matter.
                    data_ptr = input_ids.data_ptr()

                io_binding.bind_input(f'past_{i}', past_i.device.type, 0, float_type, list(past_i.size()), data_ptr)

        if attention_mask is not None:
            assert attention_mask.is_contiguous()
            io_binding.bind_input('attention_mask', attention_mask.device.type, 0, float_type,
                                  list(attention_mask.size()), attention_mask.data_ptr())

        if position_ids is not None:
            assert position_ids.is_contiguous()
            io_binding.bind_input('position_ids', position_ids.device.type, 0, numpy.longlong,
                                  list(position_ids.size()), position_ids.data_ptr())

        # Bind outputs
        for output in ort_session.get_outputs():
            output_name = output.name
            output_buffer = output_buffers[output_name]
            logger.debug(f"{output_name} device type={output_buffer.device.type} shape={list(output_buffer.size())}")
            io_binding.bind_output(output_name, output_buffer.device.type, 0, float_type, output_shapes[output_name],
                                   output_buffer.data_ptr())

        return io_binding

    @staticmethod
    def get_outputs_from_io_binding_buffer(ort_session, output_buffers, output_shapes, return_numpy=True):
        """ Copy results to cpu. Returns a list of numpy array.
        """
        ort_outputs = []
        for output in ort_session.get_outputs():
            output_name = output.name
            buffer = output_buffers[output_name]
            shape = output_shapes[output_name]
            copy_tensor = buffer[0:numpy.prod(shape)].reshape(shape).clone().detach()
            if return_numpy:
                ort_outputs.append(copy_tensor.cpu().numpy())
            else:
                ort_outputs.append(copy_tensor)
        return ort_outputs

    @staticmethod
    def onnxruntime_inference_with_binded_io(ort_session,
                                             inputs: Gpt2Inputs,
                                             output_buffers: Dict[str, torch.Tensor],
                                             output_shapes: Dict[str, List[int]],
                                             total_runs: int = 0,
                                             return_numpy: bool = True,
                                             include_copy_output_latency: bool = False):
        """ Inference with IO binding. Returns outputs, and optional latency when total_runs > 0.
        """
        logger.debug(f"start onnxruntime_inference_with_binded_io")

        # Bind inputs and outputs to onnxruntime session
        io_binding = Gpt2Helper.prepare_io_binding(ort_session, inputs.input_ids, inputs.position_ids,
                                                   inputs.attention_mask, inputs.past, output_buffers, output_shapes)

        # Run onnxruntime with io binding
        ort_session.run_with_iobinding(io_binding)

        # Copy results to cpu for verification
        ort_outputs = Gpt2Helper.get_outputs_from_io_binding_buffer(ort_session, output_buffers, output_shapes,
                                                                    return_numpy)

        if total_runs == 0:
            return ort_outputs

        latency = []
        for _ in range(total_runs):
            start = time.time()
            # Run onnxruntime with io binding
            ort_session.run_with_iobinding(io_binding)
            if include_copy_output_latency:
                _ = Gpt2Helper.get_outputs_from_io_binding_buffer(ort_session, output_buffers, output_shapes,
                                                                  return_numpy)
            latency.append(time.time() - start)

        average_latency = sum(latency) * 1000 / len(latency)
        logger.debug("OnnxRuntime with IO binding inference time = {} ms".format(format(average_latency, '.2f')))

        return ort_outputs, average_latency

    @staticmethod
    def test_parity(ort_session,
                    model,
                    device,
                    is_float16=False,
                    rtol=5e-4,
                    atol=5e-4,
                    total_test_cases=100,
                    use_io_binding=True,
                    model_class="GPT2LMHeadModel",
                    has_position_ids=True,
                    has_attention_mask=True):
        """ Generate random inputs and compare the results of PyTorch and Onnx Runtime.
        """

        config: GPT2Config = model.config

        logger.info(
            f"Running parity test (rtol={rtol}, atol={atol}, test_cases={total_test_cases}, use_io_binding={use_io_binding} model_class={model_class} is_float16={is_float16}) ..."
        )

        max_batch_size = 8
        max_past_seq_len = 4  # Do not use large number here for higher chance of hitting empty past (past_seq_len=0)
        max_seq_len = 2

        output_buffers = None
        if use_io_binding:
            max_output_shapes = Gpt2Helper.get_output_shapes(max_batch_size, max_past_seq_len, max_seq_len, config,
                                                             model_class)
            output_buffers = Gpt2Helper.get_output_buffers(max_output_shapes, device, is_float16)

        passed_test_cases = 0
        for _ in range(total_test_cases):
            sequence_length = random.randint(1, max_seq_len)
            past_sequence_length = random.randint(0, max_past_seq_len)
            batch_size = random.randint(1, max_batch_size)

            logger.debug(
                f"Running parity test for batch_size={batch_size} past_sequence_length={past_sequence_length}...")
            dummy_inputs = Gpt2Helper.get_dummy_inputs(batch_size, past_sequence_length, sequence_length,
                                                       config.num_attention_heads, config.hidden_size, config.n_layer,
                                                       config.vocab_size, device, is_float16, has_position_ids,
                                                       has_attention_mask)

            outputs = Gpt2Helper.pytorch_inference(model, dummy_inputs)
            if use_io_binding:
                ort_outputs = Gpt2Helper.onnxruntime_inference(ort_session, dummy_inputs)
            else:
                output_shapes = Gpt2Helper.get_output_shapes(batch_size, past_sequence_length, sequence_length, config,
                                                             model_class)
                ort_outputs = Gpt2Helper.onnxruntime_inference_with_binded_io(ort_session, dummy_inputs, output_buffers,
                                                                              output_shapes)

            is_all_close = Gpt2Helper.compare_outputs(outputs, ort_outputs, rtol=rtol, atol=atol)
            if is_all_close:
                passed_test_cases += 1
        logger.info(f"Parity Test Cases={total_test_cases}; Passed={passed_test_cases}")
        if passed_test_cases > 0.95 * total_test_cases:
            logger.info(f"Parity is good: passed rate={int(passed_test_cases*100/total_test_cases):.0f}%")
        return passed_test_cases == total_test_cases

    @staticmethod
    def torchscript(model, config, device, has_position_ids=True, has_attention_mask=True):
        """ JIT trace for TorchScript.
        """
        input_list = Gpt2Helper.get_dummy_inputs(batch_size=1,
                                                 past_sequence_length=1,
                                                 sequence_length=1,
                                                 num_attention_heads=config.num_attention_heads,
                                                 hidden_size=config.hidden_size,
                                                 num_layer=config.n_layer,
                                                 vocab_size=config.vocab_size,
                                                 device=device,
                                                 float16=False,
                                                 has_position_ids=has_position_ids,
                                                 has_attention_mask=has_attention_mask).to_list()
        return torch.jit.trace(model, input_list)

    @staticmethod
    def get_onnx_paths(output_dir,
                       model_name_or_path,
                       model_class: str = 'GPT2LMHeadModel',
                       has_past=True,
                       new_folder=False):
        """ Build a  path name for given model based on given attributes.
        """
        model_name = model_name_or_path
        if not re.match(r'^[\w_-]+$', model_name_or_path):  # It is not a name, shall be a path
            assert os.path.isdir(model_name_or_path)
            model_name = Path(model_name_or_path).parts[-1]

        if model_class != 'GPT2LMHeadModel':
            model_name += "_" + model_class

        if has_past:
            model_name += "_past"

        if new_folder:
            # store each model to its own directory (for external data format).
            return {
                "raw": os.path.join(os.path.join(output_dir, model_name), model_name + ".onnx"),
                "fp32": os.path.join(os.path.join(output_dir, model_name + "_fp32"), model_name + "_fp32.onnx"),
                "fp16": os.path.join(os.path.join(output_dir, model_name + "_fp16"), model_name + "_fp16.onnx"),
                "int8": os.path.join(os.path.join(output_dir, model_name + "_int8"), model_name + "_int8.onnx")
            }

        return {
            "raw": os.path.join(output_dir, model_name + ".onnx"),
            "fp32": os.path.join(output_dir, model_name + "_fp32.onnx"),
            "fp16": os.path.join(output_dir, model_name + "_fp16.onnx"),
            "int8": os.path.join(output_dir, model_name + "_int8.onnx")
        }
