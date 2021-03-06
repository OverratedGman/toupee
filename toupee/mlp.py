#!/usr/bin/python
"""
Run a MLP experiment from a yaml file

Alan Mosca
Department of Computer Science and Information Systems
Birkbeck, University of London

All code released under Apachev2.0 licensing.
"""
__docformat__ = 'restructedtext en'


import gc
import os
import sys
import time
import copy
import numpy
import scipy
import math
from pymongo import MongoClient
import json

import data
from data import Resampler, Transformer
import config 
import common
import utils

import keras
import keras.preprocessing.image

class DataHolder:
    """
    Encapsulate the train/valid/test data to achieve a few things:
    - no leakage from multiple copies
    - ensure it is always a SharedVariable
    """

    def __init__(self,dataset):
        self.orig_train_set_x = dataset[0][0]
        self.orig_train_set_y = dataset[0][1]
        self.orig_valid_set_x = dataset[1][0]
        self.orig_valid_set_y = dataset[1][1]
        self.orig_test_set_x = dataset[2][0]
        self.orig_test_set_y = dataset[2][1]
        self.train_set_x = self.orig_train_set_x
        self.train_set_y = self.orig_train_set_y
        self.valid_set_x = self.orig_valid_set_x
        self.valid_set_y = self.orig_valid_set_y
        if len(dataset) > 2:
            self.test_set_x = self.orig_test_set_x
            self.test_set_y = self.orig_test_set_y
        else:
            self.test_set_x, self.test_set_y = (None,None)
    
    def has_test(self):
        return self.test_set_x is not None

    def reshape_inputs(self,shape):
        self.train_set_x = self.orig_train_set_x.reshape([self.train_set_x.shape[0]] + shape)
        self.valid_set_x = self.orig_valid_set_x.reshape([self.valid_set_x.shape[0]] + shape)
        if self.has_test():
            self.test_set_x = self.orig_test_set_x.reshape([self.test_set_x.shape[0]] + shape)


class TrainingState:
    """
    Helps track the state of the current training.
    """
    
    def __init__(self,classifier):
        self.reset()
        self.classifier = classifier
        self.best_valid_loss = numpy.inf

    def reset(self):
        self.done_looping = False
        self.best_weights = None
        self.best_iter = 0
        self.best_epoch = 0
        self.test_score = None
        self.epoch = 0
        self.previous_minibatch_avg_cost = 1.

    def pre_iter(self):
        self.best_weights = None
        self.best_valid_loss = numpy.inf
        self.best_iter = 0
        self.best_epoch = 0
        self.test_score = 0.
        self.epoch = 0


def sequential_model(dataset, params, pretraining_set = None, model_weights = None,
        return_results = False):
    """
    Initialize the parameters and create the network.
    """

    print "loading model..."
    with open(params.model_file, 'r') as model_file:
        model_yaml = model_file.read()
    model = keras.models.model_from_yaml(model_yaml)
    total_weights = 0
    if model_weights is not None:
        model.set_weights(model_weights)

    #TODO: weight count
    print "total weight count: {0}".format(total_weights)

    results = common.Results(params)
    data_holder = DataHolder(dataset)
    data_holder.reshape_inputs(list(model.inputs[0]._keras_shape[1:]))

    rng = numpy.random.RandomState(params.random_seed)

    state = TrainingState(model)
    state.train_examples = data_holder.train_set_x.shape[0]
    state.valid_examples = data_holder.valid_set_x.shape[0]

    print "training examples: {0}".format(state.train_examples)
    print "validation examples: {0}".format(state.valid_examples)

    if data_holder.has_test():
        state.test_examples = data_holder.test_set_x.shape[0]
        print "test examples: {0}".format(state.test_examples)

    print '{0} training...'.format(params.training_method)

    start_time = time.clock()

    metrics = ['accuracy']
    if 'additional_metrics' in params.__dict__:
        metrics = metrics + additional_metrics

    model.compile(optimizer = params.update_rule,
                  loss = params.cost_function,
                  metrics = metrics
    )

    checkpointer = keras.callbacks.ModelCheckpointInMemory(verbose=1,
            monitor = 'val_loss',
            mode = 'min')
    callbacks = [checkpointer]
    if params.early_stopping is not None:
        earlyStopping=keras.callbacks.EarlyStopping(monitor='val_loss',
            patience=params.early_stopping['patience'], verbose=0, mode='auto')
        callbacks.append(earlyStopping)

    if params.online_transform is not None:
        datagen = keras.preprocessing.image.ImageDataGenerator(
            featurewise_center=False,
            samplewise_center=False,
            featurewise_std_normalization=False,
            samplewise_std_normalization=False,
            zca_whitening=False,
            rotation_range=0,
            width_shift_range=0.1,
            height_shift_range=0.1,
            horizontal_flip=True,
            vertical_flip=False)
        datagen.fit(data_holder.train_set_x)
        hist = model.fit_generator(
                            datagen.flow(
                                data_holder.train_set_x,
                                data_holder.train_set_y,
                                shuffle = params.shuffle_dataset,
                                batch_size = params.batch_size
                            ),
                            samples_per_epoch = data_holder.train_set_x.shape[0],
                            nb_epoch = params.n_epochs,
                            callbacks = callbacks,
                            validation_data = (data_holder.valid_set_x,
                                data_holder.valid_set_y),
                            test_data = (data_holder.test_set_x,
                                data_holder.test_set_y),
                           )
    else:
        hist = model.fit(data_holder.train_set_x, data_holder.train_set_y,
                  batch_size = params.batch_size,
                  nb_epoch = params.n_epochs,
                  validation_data = (data_holder.valid_set_x, data_holder.valid_set_y),
                  test_data = (data_holder.test_set_x, data_holder.test_set_y),
                  callbacks = callbacks,
                  shuffle = params.shuffle_dataset)
    model.set_weights(checkpointer.best_model)
    train_metrics = model.evaluate(data_holder.train_set_x,data_holder.train_set_y)
    valid_metrics = model.evaluate(data_holder.valid_set_x,data_holder.valid_set_y)
    if data_holder.has_test():
        test_metrics = model.evaluate(data_holder.test_set_x,data_holder.test_set_y)
    for metrics_name,metrics in (
            ('train', train_metrics),
            ('valid', valid_metrics),
            ('test', test_metrics)
        ):
        print "{0}:".format(metrics_name)
        for i in range(len(metrics)):
            print "  {0} = {1}".format(model.metrics_names[i], metrics[i])

    results.set_history(hist)
    end_time = time.clock()
    if data_holder.test_set_x is not None:
        print(('Optimization complete.\nBest valid accuracy: %f %%\n'
            'Obtained at epoch: %i\nTest accuracy: %f %%') %
              (valid_metrics[1] * 100.,
                  checkpointer.best_epoch, test_metrics[1] * 100.))
        print >> sys.stderr, ('The code for file ' +
                              os.path.split(__file__)[1] +
                              ' ran for %.2fm' % ((end_time - start_time) / 60.))
        results.set_final_observation(valid_metrics[1] * 100.,
                test_metrics[1] * 100.,
                checkpointer.best_epoch)
    else:
        results.set_final_observation(valid_metrics[1]* 100., None,
                checkpointer.best_epoch)
        print('Selection : Best valid score of {0} %'.format(
              valid_metrics[1] * 100.))

    if 'results_db' in params.__dict__ :
        if 'results_host' in params.__dict__:
            host = params.results_host
        else:
            host = None
        conn = MongoClient(host=host)
        db = conn[params.results_db]
        if 'results_table' in params.__dict__: 
            table_name = params.results_table
        else:
            table_name = 'results'
        table = db[table_name]
        print "saving results to {0}@{1}:{2}".format(params.results_db,host,table)
        table.insert(json.loads(json.dumps(results.__dict__,default=common.serialize)))
    if return_results:
        return model, results
    else:
        return model
