import os
import random
import time
import math
from functools import partial

import numpy as np
import paddle
from paddle.io import DataLoader

import paddlenlp as ppnlp
from paddlenlp.transformers import LinearDecayWithWarmup
from paddlenlp.metrics import ChunkEvaluator
from paddlenlp.datasets import load_dataset
from paddlenlp.transformers import BertForTokenClassification, BertTokenizer
from paddlenlp.data import Stack, Tuple, Pad, Dict
from ..model import NerModel

def tokenize_and_align_labels(example, tokenizer, no_entity_id,
                            max_seq_len=512):
    labels = example['labels']
    example = example['tokens']
    tokenized_input = tokenizer(
        example,
        return_length=True,
        is_split_into_words=True,
        max_seq_len=max_seq_len)

    # -2 for [CLS] and [SEP]
    if len(tokenized_input['input_ids']) - 2 < len(labels):
        labels = labels[:len(tokenized_input['input_ids']) - 2]
    tokenized_input['labels'] = [no_entity_id] + labels + [no_entity_id]
    tokenized_input['labels'] += [no_entity_id] * (
        len(tokenized_input['input_ids']) - len(tokenized_input['labels']))
    return tokenized_input

def parse_decodes(input_words, id2label, decodes, lens):
    decodes = [x for batch in decodes for x in batch]
    lens = [x for batch in lens for x in batch]

    outputs = []
    for idx, end in enumerate(lens):
        sent = "".join(input_words[idx]['tokens']) # 取出句子内容
        tags = [id2label[x] for x in decodes[idx][1:end]]
        sent_out = []
        tags_out = []
        words = ""
        for s, t in zip(sent, tags):
            if t.startswith('B-') or t == 'O':
                if len(words):
                    sent_out.append(words)
                if t.startswith('B-'):
                    tags_out.append(t.split('-')[1])
                    words = s
                else:
                    tags_out.append(t)
                    # words = ""
                words = s
            else: # 是I-标签
                words += s
        if len(sent_out) < len(tags_out): # 针对最后一个标签是B-标签实体，但是在循环中没能加进去
            sent_out.append(words)
        outputs.append(''.join(
            [str((s, t)) for s, t in zip(sent_out, tags_out)]))
    return outputs

@NerModel.register("Ner", "Paddle")
class NerPaddle(NerModel):
    def __init__(self, args, name: str = 'NerModel', ):
        super().__init__()
        self.name = name
        self.args = args

    @paddle.no_grad()
    def evaluate(self,model, loss_fct, metric, data_loader, label_num):
        model.eval()
        metric.reset()
        avg_loss, precision, recall, f1_score = 0, 0, 0, 0
        for batch in data_loader:
            input_ids, token_type_ids, length, labels = batch
            logits = model(input_ids, token_type_ids)
            loss = loss_fct(logits, labels)
            avg_loss = paddle.mean(loss)
            preds = logits.argmax(axis=2)
            num_infer_chunks, num_label_chunks, num_correct_chunks = metric.compute(
                length, preds, labels)
            metric.update(num_infer_chunks.numpy(),
                        num_label_chunks.numpy(), num_correct_chunks.numpy())
            precision, recall, f1_score = metric.accumulate()
        print("eval loss: %f, precision: %f, recall: %f, f1: %f" %
            (avg_loss, precision, recall, f1_score))
        model.train()

    def run(self):
        args = self.args
        paddle.set_device(args.device)
        if paddle.distributed.get_world_size() > 1:
            paddle.distributed.init_parallel_env()

        # Create dataset, tokenizer and dataloader.
        train_ds, test_ds = load_dataset(
            'msra_ner', splits=('train', 'test'), lazy=False)

        tokenizer = BertTokenizer.from_pretrained(args.model_name_or_path)

        label_list = train_ds.label_list
        label_num = len(label_list)
        no_entity_id = label_num - 1

        trans_func = partial(
            tokenize_and_align_labels,
            tokenizer=tokenizer,
            no_entity_id=no_entity_id,
            max_seq_len=args.max_seq_length)

        train_ds = train_ds.map(trans_func)

        ignore_label = -100

        batchify_fn = lambda samples, fn=Dict({
            'input_ids': Pad(axis=0, pad_val=tokenizer.pad_token_id),  # input
            'token_type_ids': Pad(axis=0, pad_val=tokenizer.pad_token_type_id),  # segment
            'seq_len': Stack(),  # seq_len
            'labels': Pad(axis=0, pad_val=ignore_label)  # label
        }): fn(samples)

        train_batch_sampler = paddle.io.DistributedBatchSampler(
            train_ds, batch_size=args.batch_size, shuffle=True, drop_last=True)

        train_data_loader = DataLoader(
            dataset=train_ds,
            collate_fn=batchify_fn,
            num_workers=0,
            batch_sampler=train_batch_sampler,
            return_list=True)

        test_ds = test_ds.map(trans_func)

        test_data_loader = DataLoader(
            dataset=test_ds,
            collate_fn=batchify_fn,
            num_workers=0,
            batch_size=args.batch_size,
            return_list=True)

        # Define the model netword and its loss
        model = BertForTokenClassification.from_pretrained(
            args.model_name_or_path, num_classes=label_num)
        if paddle.distributed.get_world_size() > 1:
            model = paddle.DataParallel(model)

        num_training_steps = args.max_steps if args.max_steps > 0 else len(
            train_data_loader) * args.num_train_epochs

        lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps,
                                            args.warmup_steps)

        # Generate parameter names needed to perform weight decay.
        # All bias and LayerNorm parameters are excluded.
        decay_params = [
            p.name for n, p in model.named_parameters()
            if not any(nd in n for nd in ["bias", "norm"])
        ]
        optimizer = paddle.optimizer.AdamW(
            learning_rate=lr_scheduler,
            epsilon=args.adam_epsilon,
            parameters=model.parameters(),
            weight_decay=args.weight_decay,
            apply_decay_param_fun=lambda x: x in decay_params)

        loss_fct = paddle.nn.loss.CrossEntropyLoss(ignore_index=ignore_label)

        metric = ChunkEvaluator(label_list=label_list)

        global_step = 0
        last_step = args.num_train_epochs * len(train_data_loader)
        tic_train = time.time()
        for epoch in range(args.num_train_epochs):
            for step, batch in enumerate(train_data_loader):
                global_step += 1
                input_ids, token_type_ids, _, labels = batch
                logits = model(input_ids, token_type_ids)
                loss = loss_fct(logits, labels)
                avg_loss = paddle.mean(loss)
                if global_step % args.logging_steps == 0:
                    print(
                        "global step %d, epoch: %d, batch: %d, loss: %f, speed: %.2f step/s"
                        % (global_step, epoch, step, avg_loss,
                        args.logging_steps / (time.time() - tic_train)))
                    tic_train = time.time()
                avg_loss.backward()
                optimizer.step()
                lr_scheduler.step()
                optimizer.clear_grad()
                if global_step % args.save_steps == 0 or global_step == last_step:
                    if paddle.distributed.get_rank() == 0:
                        self.evaluate(model, loss_fct, metric, test_data_loader,
                                label_num)
                        paddle.save(model.state_dict(),
                                    os.path.join(args.output_dir,
                                                "model_%d.pdparams" % global_step))
