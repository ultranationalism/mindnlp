# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
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
""" Testing suite for the PyTorch ALIGN model. """


import inspect
import tempfile
import unittest

import requests

import numpy as np
from mindnlp.transformers import AlignConfig, AlignProcessor, AlignTextConfig, AlignVisionConfig
from mindnlp.utils.testing_utils import (
    require_mindspore,
    require_vision,
    slow,
)
from mindnlp.utils import is_mindspore_available, is_vision_available

from ...test_configuration_common import ConfigTester
from ...test_modeling_common import (
    ModelTesterMixin,
    _config_zero_init,
    floats_tensor,
    ids_tensor,
    random_attention_mask,
)
# from ...test_pipeline_mixin import PipelineTesterMixin

if is_mindspore_available():
    import mindspore

    from mindnlp.transformers import (
        AlignModel,
        AlignTextModel,
        AlignVisionModel,
    )
    from mindnlp.transformers.models.align.modeling_align import ALIGN_PRETRAINED_MODEL_ARCHIVE_LIST

mindspore.set_context(pynative_synchronize=True)

if is_vision_available():
    from PIL import Image


class AlignVisionModelTester:
    def __init__(
        self,
        parent,
        batch_size=12,
        image_size=32,
        num_channels=3,
        kernel_sizes=[3, 3, 5],
        in_channels=[32, 16, 24],
        out_channels=[16, 24, 30],
        hidden_dim=64,
        strides=[1, 1, 2],
        num_block_repeats=[1, 1, 2],
        expand_ratios=[1, 6, 6],
        is_training=True,
        hidden_act="gelu",
    ):
        self.parent = parent
        self.batch_size = batch_size
        self.image_size = image_size
        self.num_channels = num_channels
        self.kernel_sizes = kernel_sizes
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.hidden_dim = hidden_dim
        self.strides = strides
        self.num_block_repeats = num_block_repeats
        self.expand_ratios = expand_ratios
        self.is_training = is_training
        self.hidden_act = hidden_act

    def prepare_config_and_inputs(self):
        pixel_values = floats_tensor([self.batch_size, self.num_channels, self.image_size, self.image_size])
        config = self.get_config()

        return config, pixel_values

    def get_config(self):
        return AlignVisionConfig(
            num_channels=self.num_channels,
            kernel_sizes=self.kernel_sizes,
            in_channels=self.in_channels,
            out_channels=self.out_channels,
            hidden_dim=self.hidden_dim,
            strides=self.strides,
            num_block_repeats=self.num_block_repeats,
            expand_ratios=self.expand_ratios,
            hidden_act=self.hidden_act,
        )

    def create_and_check_model(self, config, pixel_values):
        model = AlignVisionModel(config=config)

        model.set_train(False)
        result = model(pixel_values)

        patch_size = self.image_size // 4
        self.parent.assertEqual(
            result.last_hidden_state.shape, (self.batch_size, config.hidden_dim, patch_size, patch_size)
        )
        self.parent.assertEqual(result.pooler_output.shape, (self.batch_size, config.hidden_dim))

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()
        config, pixel_values = config_and_inputs
        inputs_dict = {"pixel_values": pixel_values}
        return config, inputs_dict


@require_mindspore
class AlignVisionModelTest(ModelTesterMixin, unittest.TestCase):
    """
    Here we also overwrite some of the tests of test_modeling_common.py, as ALIGN does not use input_ids, inputs_embeds,
    attention_mask and seq_length.
    """

    all_model_classes = (AlignVisionModel,) if is_mindspore_available() else ()
    fx_compatible = False
    test_pruning = False
    test_resize_embeddings = False
    test_head_masking = False
    has_attentions = False

    def setUp(self):
        self.model_tester = AlignVisionModelTester(self)
        self.config_tester = ConfigTester(
            self, config_class=AlignVisionConfig, has_text_modality=False, hidden_size=37
        )

    def test_config(self):
        self.create_and_test_config_common_properties()
        self.config_tester.create_and_test_config_to_json_string()
        self.config_tester.create_and_test_config_to_json_file()
        self.config_tester.create_and_test_config_from_and_save_pretrained()
        self.config_tester.create_and_test_config_with_num_labels()
        self.config_tester.check_config_can_be_init_without_params()
        self.config_tester.check_config_arguments_init()

    def create_and_test_config_common_properties(self):
        return

    @unittest.skip(reason="AlignVisionModel does not use inputs_embeds")
    def test_inputs_embeds(self):
        pass

    @unittest.skip(reason="AlignVisionModel does not support input and output embeddings")
    def test_model_common_attributes(self):
        pass

    def test_forward_signature(self):
        config, _ = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            model = model_class(config)
            signature = inspect.signature(model.forward)
            # signature.parameters is an OrderedDict => so arg_names order is deterministic
            arg_names = [*signature.parameters.keys()]

            expected_arg_names = ["pixel_values"]
            self.assertListEqual(arg_names[:1], expected_arg_names)

    def test_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model(*config_and_inputs)

    def test_hidden_states_output(self):
        def check_hidden_states_output(inputs_dict, config, model_class):
            model = model_class(config)
    
            model.set_train(False)

            outputs = model(**self._prepare_for_class(inputs_dict, model_class))

            hidden_states = outputs.encoder_hidden_states if config.is_encoder_decoder else outputs.hidden_states
            num_blocks = sum(config.num_block_repeats) * 4
            self.assertEqual(len(hidden_states), num_blocks)

            self.assertListEqual(
                list(hidden_states[0].shape[-2:]),
                [self.model_tester.image_size // 2, self.model_tester.image_size // 2],
            )

        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            inputs_dict["output_hidden_states"] = True
            check_hidden_states_output(inputs_dict, config, model_class)

            # check that output_hidden_states also work using config
            del inputs_dict["output_hidden_states"]
            config.output_hidden_states = True

            check_hidden_states_output(inputs_dict, config, model_class)

    def test_training(self):
        pass

    def test_training_gradient_checkpointing(self):
        pass

    @unittest.skip(
        reason="This architecure seem to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing_use_reentrant(self):
        pass

    @unittest.skip(
        reason="This architecure seem to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing_use_reentrant_false(self):
        pass

    @slow
    def test_model_from_pretrained(self):
        for model_name in ALIGN_PRETRAINED_MODEL_ARCHIVE_LIST[:1]:
            model = AlignVisionModel.from_pretrained(model_name)
            self.assertIsNotNone(model)


class AlignTextModelTester:
    def __init__(
        self,
        parent,
        batch_size=12,
        seq_length=7,
        is_training=True,
        use_input_mask=True,
        use_token_type_ids=True,
        vocab_size=99,
        hidden_size=32,
        num_hidden_layers=2,
        num_attention_heads=4,
        intermediate_size=37,
        hidden_act="gelu",
        hidden_dropout_prob=0.1,
        attention_probs_dropout_prob=0.1,
        max_position_embeddings=512,
        type_vocab_size=16,
        type_sequence_label_size=2,
        initializer_range=0.02,
        scope=None,
    ):
        self.parent = parent
        self.batch_size = batch_size
        self.seq_length = seq_length
        self.is_training = is_training
        self.use_input_mask = use_input_mask
        self.use_token_type_ids = use_token_type_ids
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.intermediate_size = intermediate_size
        self.hidden_act = hidden_act
        self.hidden_dropout_prob = hidden_dropout_prob
        self.attention_probs_dropout_prob = attention_probs_dropout_prob
        self.max_position_embeddings = max_position_embeddings
        self.type_vocab_size = type_vocab_size
        self.type_sequence_label_size = type_sequence_label_size
        self.initializer_range = initializer_range
        self.scope = scope

    def prepare_config_and_inputs(self):
        input_ids = ids_tensor([self.batch_size, self.seq_length], self.vocab_size)

        input_mask = None
        if self.use_input_mask:
            input_mask = random_attention_mask([self.batch_size, self.seq_length])

        token_type_ids = None
        if self.use_token_type_ids:
            token_type_ids = ids_tensor([self.batch_size, self.seq_length], self.type_vocab_size)

        config = self.get_config()

        return config, input_ids, token_type_ids, input_mask

    def get_config(self):
        return AlignTextConfig(
            vocab_size=self.vocab_size,
            hidden_size=self.hidden_size,
            num_hidden_layers=self.num_hidden_layers,
            num_attention_heads=self.num_attention_heads,
            intermediate_size=self.intermediate_size,
            hidden_act=self.hidden_act,
            hidden_dropout_prob=self.hidden_dropout_prob,
            attention_probs_dropout_prob=self.attention_probs_dropout_prob,
            max_position_embeddings=self.max_position_embeddings,
            type_vocab_size=self.type_vocab_size,
            is_decoder=False,
            initializer_range=self.initializer_range,
        )

    def create_and_check_model(self, config, input_ids, token_type_ids, input_mask):
        model = AlignTextModel(config=config)

        model.set_train(False)
        result = model(input_ids, attention_mask=input_mask, token_type_ids=token_type_ids)
        result = model(input_ids, token_type_ids=token_type_ids)
        result = model(input_ids)
        self.parent.assertEqual(result.last_hidden_state.shape, (self.batch_size, self.seq_length, self.hidden_size))
        self.parent.assertEqual(result.pooler_output.shape, (self.batch_size, self.hidden_size))

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()
        (
            config,
            input_ids,
            token_type_ids,
            input_mask,
        ) = config_and_inputs
        inputs_dict = {"input_ids": input_ids, "token_type_ids": token_type_ids, "attention_mask": input_mask}
        return config, inputs_dict


@require_mindspore
class AlignTextModelTest(ModelTesterMixin, unittest.TestCase):
    all_model_classes = (AlignTextModel,) if is_mindspore_available() else ()
    fx_compatible = False
    test_pruning = False
    test_head_masking = False

    def setUp(self):
        self.model_tester = AlignTextModelTester(self)
        self.config_tester = ConfigTester(self, config_class=AlignTextConfig, hidden_size=37)

    def test_config(self):
        self.config_tester.run_common_tests()

    def test_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model(*config_and_inputs)

    def test_training(self):
        pass

    def test_training_gradient_checkpointing(self):
        pass

    @unittest.skip(
        reason="This architecure seem to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing_use_reentrant(self):
        pass

    @unittest.skip(
        reason="This architecure seem to not compute gradients properly when using GC, check: https://github.com/huggingface/transformers/pull/27124"
    )
    def test_training_gradient_checkpointing_use_reentrant_false(self):
        pass

    @unittest.skip(reason="ALIGN does not use inputs_embeds")
    def test_inputs_embeds(self):
        pass

    @unittest.skip(reason="AlignTextModel has no base class and is not available in MODEL_MAPPING")
    def test_save_load_fast_init_from_base(self):
        pass

    @unittest.skip(reason="AlignTextModel has no base class and is not available in MODEL_MAPPING")
    def test_save_load_fast_init_to_base(self):
        pass

    @slow
    def test_model_from_pretrained(self):
        for model_name in ALIGN_PRETRAINED_MODEL_ARCHIVE_LIST[:1]:
            model = AlignTextModel.from_pretrained(model_name)
            self.assertIsNotNone(model)


class AlignModelTester:
    def __init__(self, parent, text_kwargs=None, vision_kwargs=None, is_training=True):
        if text_kwargs is None:
            text_kwargs = {}
        if vision_kwargs is None:
            vision_kwargs = {}

        self.parent = parent
        self.text_model_tester = AlignTextModelTester(parent, **text_kwargs)
        self.vision_model_tester = AlignVisionModelTester(parent, **vision_kwargs)
        self.batch_size = self.text_model_tester.batch_size  # need bs for batching_equivalence test
        self.is_training = is_training

    def prepare_config_and_inputs(self):
        test_config, input_ids, token_type_ids, input_mask = self.text_model_tester.prepare_config_and_inputs()
        vision_config, pixel_values = self.vision_model_tester.prepare_config_and_inputs()

        config = self.get_config()

        return config, input_ids, token_type_ids, input_mask, pixel_values

    def get_config(self):
        return AlignConfig.from_text_vision_configs(
            self.text_model_tester.get_config(), self.vision_model_tester.get_config(), projection_dim=64
        )

    def create_and_check_model(self, config, input_ids, token_type_ids, attention_mask, pixel_values):
        model = AlignModel(config).set_train(False)
        result = model(input_ids, pixel_values, attention_mask, token_type_ids)
        self.parent.assertEqual(
            result.logits_per_image.shape, (self.vision_model_tester.batch_size, self.text_model_tester.batch_size)
        )
        self.parent.assertEqual(
            result.logits_per_text.shape, (self.text_model_tester.batch_size, self.vision_model_tester.batch_size)
        )

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()
        config, input_ids, token_type_ids, input_mask, pixel_values = config_and_inputs
        inputs_dict = {
            "input_ids": input_ids,
            "token_type_ids": token_type_ids,
            "attention_mask": input_mask,
            "pixel_values": pixel_values,
            "return_loss": True,
        }
        return config, inputs_dict


@require_mindspore
class AlignModelTest(ModelTesterMixin, unittest.TestCase):
    all_model_classes = (AlignModel,) if is_mindspore_available() else ()
    pipeline_model_mapping = {"feature-extraction": AlignModel} if is_mindspore_available() else {}
    fx_compatible = False
    test_head_masking = False
    test_pruning = False
    test_resize_embeddings = False
    test_attention_outputs = False

    def setUp(self):
        self.model_tester = AlignModelTester(self)

    def test_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model(*config_and_inputs)

    @unittest.skip(reason="Start to fail after using torch `cu118`.")
    def test_multi_gpu_data_parallel_forward(self):
        super().test_multi_gpu_data_parallel_forward()

    @unittest.skip(reason="Hidden_states is tested in individual model tests")
    def test_hidden_states_output(self):
        pass

    @unittest.skip(reason="Inputs_embeds is tested in individual model tests")
    def test_inputs_embeds(self):
        pass

    @unittest.skip(reason="Retain_grad is tested in individual model tests")
    def test_retain_grad_hidden_states_attentions(self):
        pass

    @unittest.skip(reason="AlignModel does not have input/output embeddings")
    def test_model_common_attributes(self):
        pass

    # override as the `temperature` parameter initilization is different for ALIGN
    def test_initialization(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        configs_no_init = _config_zero_init(config)
        for model_class in self.all_model_classes:
            model = model_class(config=configs_no_init)
            for name, param in model.parameters_and_names():
                if param.requires_grad:
                    # check if `temperature` is initilized as per the original implementation
                    if name == "temperature":
                        self.assertAlmostEqual(
                            param.data.item(),
                            1.0,
                            delta=1e-3,
                            msg=f"Parameter {name} of model {model_class} seems not properly initialized",
                        )
                    elif name == "text_projection.weight":
                        self.assertTrue(
                            -1.0 <= ((param.data.mean() * 1e9).round() / 1e9).item() <= 1.0,
                            msg=f"Parameter {name} of model {model_class} seems not properly initialized",
                        )
                    else:
                        self.assertIn(
                            ((param.data.mean() * 1e9).round() / 1e9).item(),
                            [0.0, 1.0],
                            msg=f"Parameter {name} of model {model_class} seems not properly initialized",
                        )

    def test_load_vision_text_config(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        # Save AlignConfig and check if we can load AlignVisionConfig from it
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            config.save_pretrained(tmp_dir_name)
            vision_config = AlignVisionConfig.from_pretrained(tmp_dir_name)
            self.assertDictEqual(config.vision_config.to_dict(), vision_config.to_dict())

        # Save AlignConfig and check if we can load AlignTextConfig from it
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            config.save_pretrained(tmp_dir_name)
            text_config = AlignTextConfig.from_pretrained(tmp_dir_name)
            self.assertDictEqual(config.text_config.to_dict(), text_config.to_dict())

    @slow
    def test_model_from_pretrained(self):
        for model_name in ALIGN_PRETRAINED_MODEL_ARCHIVE_LIST[:1]:
            model = AlignModel.from_pretrained(model_name)
            self.assertIsNotNone(model)


# We will verify our results on an image of cute cats
def prepare_img():
    url = "http://images.cocodataset.org/val2017/000000039769.jpg"
    im = Image.open(requests.get(url, stream=True).raw)
    return im


@require_vision
@require_mindspore
class AlignModelIntegrationTest(unittest.TestCase):
    @slow
    def test_inference(self):
        model_name = "kakaobrain/align-base"
        model = AlignModel.from_pretrained(model_name)
        processor = AlignProcessor.from_pretrained(model_name)

        image = prepare_img()
        texts = ["a photo of a cat", "a photo of a dog"]
        inputs = processor(text=texts, images=image, return_tensors="ms")

        # forward pass
        outputs = model(**inputs)

        # verify the logits
        self.assertEqual(
            outputs.logits_per_image.shape,
            (inputs.pixel_values.shape[0], inputs.input_ids.shape[0]),
        )
        self.assertEqual(
            outputs.logits_per_text.shape,
            (inputs.input_ids.shape[0], inputs.pixel_values.shape[0]),
        )
        expected_logits = mindspore.tensor([[9.7093, 3.4679]])
        print(outputs.logits_per_image.asnumpy())
        self.assertTrue(np.allclose(outputs.logits_per_image.asnumpy(), expected_logits.asnumpy(), atol=1e-3))
