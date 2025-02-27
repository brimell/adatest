import numpy as np
import re
import logging
import uuid
import itertools
import shap
from ._model import Model
import adatest

log = logging.getLogger(__name__)

class Scorer():
    def __new__(cls, model, *args, **kwargs):
        """ If we are wrapping an object that is already a Scorer, we just return it.
        """
        if shap.utils.safe_isinstance(model, "adatest.Scorer"):
            return model
        else:
            return super().__new__(cls)
    
    def __init__(self, model, label = [],hypothesis_template=None,local=True,multi_class=False,th=0.95):
        """ Auto detect the model type and subclass to the right scorer object.
        """

        # ensure we have a model of type Model
        if shap.utils.safe_isinstance(getattr(self, "model", None), "adatest.Model") or shap.utils.safe_isinstance(getattr(self, "model", None), "shap.models.Model"):
            pass
        elif shap.utils.safe_isinstance(model, "adatest.Model") or shap.utils.safe_isinstance(model, "shap.models.Model"):
            self.model = model
        else:
            self.model = Model(model)

        # If we are in the base class we need to pick the right specialized subclass to become
        if self.__class__ is Scorer:

            # finish early if we are wrapping an object that is already a Scorer (__new__ will have already done the work)
            if shap.utils.safe_isinstance(model, "adatest.Scorer"):
                return
            if label and local:
                self.__class__ = EntailmentScorer
                EntailmentScorer.__init__(self, model,output_names=label,label=label,hypothesis_template=hypothesis_template)
            elif label and not local:
                self.__class__ = EntailmentAPIScorer
                EntailmentAPIScorer.__init__(self, model,output_names=label,label=label,hypothesis_template=hypothesis_template,multi_class=multi_class,th=th)
            # see if we are scoring a generator or a classifier
            else:
                out = self.model(["string 1", "string 2"])
                if isinstance(out[0], str):
                    self.__class__ = GeneratorScorer
                    GeneratorScorer.__init__(self, model)
                else:
                    self.__class__ = ClassifierScorer
                    ClassifierScorer.__init__(self, model)
            

class DummyScorer(Scorer):
    def __init__(self):
        self._id = uuid.uuid4().hex
    def __call__(self, tests):
        out = []
        for k, test in tests.iterrows():
            try:
                score = float(test.value2)
            except:
                score = np.nan
            out.append(score)
        return np.array(out)

class ClassifierScorer(Scorer):
    """ Wraps a text classification model and defines a callable scorer that returns a score value for any input/output pair.

    Positive scores indicate test failures, positive scores indicate tests that pass. For example if we wrap
    a text sentiment classifer the `scorer(TestTree([("this is great!", "should be", "POSITIVE")]))` will return
    a large positive value indicating that the model is very likely to correctly produce that output when given
    that input.
    """

    def __init__(self, model, top_probs=20, output_names=None):
        """ Create a new scorer given a model that returns a probability vector for each input string.
        
        Parameters:
        -----------
        model : callable
            A model that is callable with a single argument (which is a list of strings) and returns a matrix of outputs.

        top_probs : int
            The number of top output probabilities to consider when scoring tests. This is used to reduce the number of
            input/output pairs that are passed to the local topic labeling model (and so save compute).

        output_names : list of strings
            A list of strings that correspond to the outputs of the model. If None, model.output_names is used.
        """
        super().__init__(model)

        # extract output names from the model if they are not provided directly
        if output_names is None and getattr(self, "output_names", None) is None:
            self.output_names = self.model.output_names
        elif output_names is not None:
            self.output_names = output_names
        elif not hasattr(self, "output_names"):
            self.output_names = None
        
        if not callable(self.output_names):
            self._output_name_to_index = {v: i for i, v in enumerate(self.output_names)}
        self.top_probs = top_probs

    def __call__(self, tests, eval_ids):
        """ Compute the scores (and model outputs) for the tests matching the given ids.

        Parameters
        ----------
        tests : TestTree
            A test tree for scoring. Note this should be the full test tree since it defines the local topic label
            models used for scoring.

        eval_ids : list of strings
            The ids of the tests to score.
        """
        
        # expand templates in the test tree
        eval_inputs = []
        eval_inds = []
        for i, id in enumerate(eval_ids):
            test = tests.loc[id]
            template_expansions = expand_template(test.input)
            for expansion in template_expansions:
                eval_inputs.append(expansion)
                eval_inds.append(i)

        # run the model
        try:
            model_out = self.model(eval_inputs)
        except Exception as e:
            model_out = np.zeros((len(eval_inputs), len(self.model.output_names))) * np.nan # TODO: remove this hack after the user study
            log.error(e)
            log.error(eval_inputs)
            log.error("The model threw an exception when evaluating inputs! We are patching this disaster with np.nan for the sake of the user study!")

        # compute the output strings and probabilites for each output in template form
        out_strings = [[] for _ in range(len(eval_ids))]
        out_probs = [[] for _ in range(len(eval_ids))]
        i = 0
        while i < len(model_out):
            out_strings[eval_inds[i]].append(self.model.output_names[np.argmax(model_out[i])])
            out_probs[eval_inds[i]].append(model_out[i])
            i += 1
        for i in set(eval_inds):
            out_strings[i] = "|".join(out_strings[i]) # template outputs are joined by |
            out_probs[i] = np.column_stack(out_probs[i]) # the probability of a set of items is the prob of the min item

        # compute the embeddings as a batch (this fills a cache we will use when scoring below)
        adatest.embed(list(tests.loc[eval_ids, "input"]))

        # score all the tests
        scores = []
        outputs = []
        for i, ind in enumerate(eval_inds):
            outputs.append(out_strings[ind])
            scores.append(self._score_test(tests, eval_ids[ind], out_probs[ind], self.top_probs))

        return outputs,scores
 
    def _score_test(self, tests, id, probs, top_probs):
        test = tests.loc[id]
        fail_prob = 0
        pass_prob = 0

        # if this is not a templated test
        if probs.shape[1] == 1:
            inds = np.argsort(probs[:,0])[::-1]
            for ind in inds[:top_probs]:

                # Scott: we could use any manually given labels when possible, but then that would make the score depend on the label 
                # and so we would either need to save the full output of the model or recompute every time
                # if self.model.output_names[ind] == test["output"] and test["labeler"] != "imputed":
                #     label = test["label"]
                
                # we use the local topic model to predict the label
                label = tests.topic_labeling_model(test.topic)(test.input, self.model.output_names[ind])
                if label == "fail":
                    fail_prob += probs[ind, 0]
                elif label == "pass":
                    pass_prob += probs[ind, 0]

            if not (fail_prob + pass_prob > 0):
                return np.nan
            else:
                return fail_prob / (pass_prob + fail_prob)
        else:
            raise NotImplementedError("TODO: implement classifer scoring for templated tests")

class GeneratorScorer(Scorer):
    """ Wraps a text generation model as a callable scorer that can be applied to a test tree.
    """

    def __init__(self, model):
        """ Create a new scorer for a generative text model.
        
        Parameters:
        -----------
        model : callable
            A model that is callable with a single argument (which is a list of strings) and returns a list of strings.
        """
        super().__init__(model)

        # we don't want to re-init a class if init has alrady been done (this can happen when Scorer(maybe_scorer) is called)
        if hasattr(self, "_id"):
            return # already initialized

    def __call__(self, tests, eval_ids):
        """ Score a set of tests.

        Parameters
        ----------
        tests : TestTree or DataFrame
            A dataframe of tests.

        eval_ids : list of strings
            The evaluation IDs to use.
        """

        # determine which rows we need to evaluate
        eval_inputs = []
        eval_inds = []
        for i, id in enumerate(eval_ids):
            template_expansions = expand_template(tests.loc[id, "input"])
            for expansion in template_expansions:
                eval_inputs.append(expansion)
                eval_inds.append(i)

        # run the model on the rows we need to evaluate
        try:
            model_out = self.model(eval_inputs)
        except Exception as e:
            model_out = [""] * len(eval_inputs) # TODO: remove this hack after the user study
            log.error(e)
            log.error(eval_inputs)
            log.error("The model threw an exception when evaluating inputs! We are patching this disaster with np.nan for the sake of the user study!")

        # compute the output strings for each output
        out_strings = [[] for _ in range(len(eval_ids))]
        i = 0
        while i < len(model_out):
            out_strings[eval_inds[i]].append(str(model_out[i]))
            i += 1
        for i in set(eval_inds):
            out_strings[i] = "|".join(out_strings[i]) # template outputs are joined by |

        scores = []
        outputs = []
        for i, ind in enumerate(eval_inds):
            outputs.append(out_strings[ind])
            scores.append(self._score_test(tests, eval_ids[ind], out_strings[ind]))

        return outputs,scores

    def _score_test(self, tests, id, output):
        test = tests.loc[id]

        label = tests.topic_labeling_model(test.topic)(test.input, output)

        if label == "pass":
            return 0.0
        else:
            return 1.0

class RawScorer(Scorer):
    """ Wraps a model that directly outputs a score each input as a callable scorer.

    The score from the model should be in the range [0,1] with higher scores indicating failures
    (or just more interesting behavior).
    """

    def __init__(self, model):
        """ Create a new scorer given a model that returns a bounded real value for each input string.
        
        Parameters:
        -----------
        model : callable
            A model that is callable with a single argument (which is a list of strings) and returns a vector of score in the range [0,1].
        """
        super().__init__(model)

    def __call__(self, tests, eval_ids):
        """ Compute the scores (and model outputs) for the tests matching the given ids.

        Parameters
        ----------
        tests : TestTree
            A test tree for scoring. Note this should be the full test tree since it defines the local topic label
            models used for scoring.

        eval_ids : list of strings
            The ids of the tests to score.
        """
        
        # expand templates in the test tree
        eval_inputs = []
        eval_inds = []
        for i, id in enumerate(eval_ids):
            test = tests.loc[id]
            template_expansions = expand_template(test.input)
            for expansion in template_expansions:
                eval_inputs.append(expansion)
                eval_inds.append(i)

        # run the model
        try:
            model_out = self.model(eval_inputs)
        except Exception as e:
            model_out = np.zeros(len(eval_inputs)) * np.nan # TODO: remove this hack after the user study
            log.error(e)
            log.error(eval_inputs)
            log.error("The model threw an exception when evaluating inputs! We are patching this disaster with np.nan for the sake of the user study!")

        # compute the output strings and scores for each output in template form
        out_strings = [[] for _ in range(len(eval_ids))]
        out_scores = [[] for _ in range(len(eval_ids))]
        i = 0
        while i < len(model_out):
            out_strings[eval_inds[i]].append(str(np.round(model_out[i], 6))) # convert float to string with precision of 6
            out_scores[eval_inds[i]].append(model_out[i])
            i += 1
        for i in eval_inds:
            out_strings[i] = "|".join(out_strings[i]) # template outputs are joined by |
            out_scores[i] = np.max(out_scores[i]) # the score of a set of items is the score of the max item

        # score all the tests
        scores = []
        outputs = []
        for i, ind in enumerate(eval_inds):
            outputs.append(out_strings[ind])
            scores.append(out_scores[ind])

        return outputs,scores

def expand_template(s, keep_braces=False):
    """ Expand a template string into a list of strings.
    """
    # parts = []
    # for s in strings:
    matches = re.findall("{[^}]*}", s)
    s = re.sub("{[^}]*}", "{}", s)
    template_groups = [str(m)[1:-1].split("|") for m in matches]
    try:
        if keep_braces:
            return [s.format(*['{{{p}}}' for p in parts]) for parts in itertools.product(*template_groups)]
        else:
            return [s.format(*parts) for parts in itertools.product(*template_groups)]
    except ValueError:
        return [s] # we return the template not filled in if it is invalid

def clean_template(s):
    """ This removes duplicate template entries.
    """
    matches = re.findall("{[^}]*}", s)
    s = re.sub("{[^}]*}", "{}", s)
    template_groups = [str(m)[1:-1].split("|") for m in matches]
    clean_groups = ["{"+"|".join(list({v: None for v in g}.keys()))+"}" for g in template_groups]
    try:
        return s.format(*clean_groups)
    except ValueError:
        return s # we return the template not cleaned in if it is invalid

class EntailmentScorer(Scorer):
    """ Wraps a text classification model and defines a callable scorer that returns a score value for any input/output pair.

    Positive scores indicate test failures, positive scores indicate tests that pass. For example if we wrap
    a text sentiment classifer the `scorer(TestTree([("this is great!", "should be", "POSITIVE")]))` will return
    a large positive value indicating that the model is very likely to correctly produce that output when given
    that input.
    """

    def __init__(self, model, top_probs=20, output_names=None,label=[],hypothesis_template = "This example is {}."):
        """ Create a new scorer given a model that returns a probability vector for each input string.
        
        Parameters:
        -----------
        model : callable
            A model that is callable with a single argument (which is a list of strings) and returns a matrix of outputs.

        top_probs : int
            The number of top output probabilities to consider when scoring tests. This is used to reduce the number of
            input/output pairs that are passed to the local topic labeling model (and so save compute).

        output_names : list of strings
            A list of strings that correspond to the outputs of the model. If None, model.output_names is used.
        """
        super().__init__(model)

        # extract output names from the model if they are not provided directly
        if output_names is None and getattr(self, "output_names", None) is None:
            self.output_names = self.model.output_names
        elif output_names is not None:
            self.output_names = output_names
        elif not hasattr(self, "output_names"):
            self.output_names = None
        self.label = label
        self.hypothesis_template = hypothesis_template
        if not callable(self.output_names):
            self._output_name_to_index = {v: i for i, v in enumerate(self.output_names)}
        self.top_probs = top_probs
        #print(self.output_names)
        self.model.output_names = self.output_names

    def __call__(self, tests, eval_ids):
        """ Compute the scores (and model outputs) for the tests matching the given ids.

        Parameters
        ----------
        tests : TestTree
            A test tree for scoring. Note this should be the full test tree since it defines the local topic label
            models used for scoring.

        eval_ids : list of strings
            The ids of the tests to score.
        """
        
        # expand templates in the test tree
        eval_inputs = []
        eval_inds = []
        
        for i, id in enumerate(eval_ids):
            test = tests.loc[id]
            template_expansions = expand_template(test.input)
            for expansion in template_expansions:
                eval_inputs.append(expansion)
                eval_inds.append(i)
        #print("evalinp",eval_inputs,eval_inputs)
        #print("ent" ,eval_inputs,self.label)
        #print(self.model)
        # run the model
        try:
            model_out = self.model(sequences= eval_inputs,candidate_labels=self.label,hypothesis_template = self.hypothesis_template)
        except Exception as e:
            model_out = np.zeros((len(eval_inputs), len(self.model.output_names))) * np.nan # TODO: remove this hack after the user study
            log.error(e)
            log.error(eval_inputs)
            log.error("The model threw an exception when evaluating inputs! We are patching this disaster with np.nan for the sake of the user study!")
        print(model_out)
        # compute the output strings and probabilites for each output in template form
        out_strings = [[] for _ in range(len(eval_ids))]
        out_probs = [[] for _ in range(len(eval_ids))]
        i = 0
        while i < len(model_out):
            out_strings[eval_inds[i]].append(model_out[i]["labels"][np.argmax(model_out[i]["scores"])])
            out_probs[eval_inds[i]].append(model_out[i]["scores"])
            i += 1
        for i in set(eval_inds):
            out_strings[i] = "|".join(out_strings[i]) # template outputs are joined by |
            out_probs[i] = np.column_stack(out_probs[i]) # the probability of a set of items is the prob of the min item

        # compute the embeddings as a batch (this fills a cache we will use when scoring below)
        adatest.embed(list(tests.loc[eval_ids, "input"]))

        # score all the tests
        scores = []
        outputs = []
        for i, ind in enumerate(eval_inds):
            print(i , ind)
            outputs.append(out_strings[ind])
            scores.append(self._score_test(tests, eval_ids[ind], out_probs[ind], self.top_probs))

        return outputs,scores
 
    def _score_test(self, tests, id, probs, top_probs):
        test = tests.loc[id]
        fail_prob = 0
        pass_prob = 0

        # if this is not a templated test
        if probs.shape[1] == 1:
            inds = np.argsort(probs[:,0])[::-1]
            for ind in inds[:top_probs]:

                # Scott: we could use any manually given labels when possible, but then that would make the score depend on the label 
                # and so we would either need to save the full output of the model or recompute every time
                # if self.model.output_names[ind] == test["output"] and test["labeler"] != "imputed":
                #     label = test["label"]
                
                # we use the local topic model to predict the label
                label = tests.topic_labeling_model(test.topic)(test.input, self.model.output_names[ind])
                if label == "fail":
                    fail_prob += probs[ind, 0]
                elif label == "pass":
                    pass_prob += probs[ind, 0]

            if not (fail_prob + pass_prob > 0):
                return np.nan
            else:
                return fail_prob / (pass_prob + fail_prob)
        else:
            raise NotImplementedError("TODO: implement classifer scoring for templated tests")

class EntailmentAPIScorer(Scorer):
    """ Wraps a text classification model and defines a callable scorer that returns a score value for any input/output pair.

    Positive scores indicate test failures, positive scores indicate tests that pass. For example if we wrap
    a text sentiment classifer the `scorer(TestTree([("this is great!", "should be", "POSITIVE")]))` will return
    a large positive value indicating that the model is very likely to correctly produce that output when given
    that input.
    """

    def __init__(self, model, top_probs=20, output_names=None,label=[],hypothesis_template = "This example is {}.",multi_class=False,th=0.95):
        """ Create a new scorer given a model that returns a probability vector for each input string.
        
        Parameters:
        -----------
        model : callable
            A model that is callable with a single argument (which is a list of strings) and returns a matrix of outputs.

        top_probs : int
            The number of top output probabilities to consider when scoring tests. This is used to reduce the number of
            input/output pairs that are passed to the local topic labeling model (and so save compute).

        output_names : list of strings
            A list of strings that correspond to the outputs of the model. If None, model.output_names is used.
        """
        super().__init__(model)

        # extract output names from the model if they are not provided directly
        if output_names is None and getattr(self, "output_names", None) is None:
            self.output_names = self.model.output_names
        elif output_names is not None:
            self.output_names = output_names
        elif not hasattr(self, "output_names"):
            self.output_names = None
        self.label = self.output_names #dirty way to pass labels
        self.hypothesis_template = hypothesis_template
        if not callable(self.output_names):
            self._output_name_to_index = {v: i for i, v in enumerate(self.output_names)}
        self.top_probs = top_probs
        self.model.output_names = self.output_names
        self.multi_class = multi_class
        self.th = th

    def __call__(self, tests, eval_ids):
        """ Compute the scores (and model outputs) for the tests matching the given ids.

        Parameters
        ----------
        tests : TestTree
            A test tree for scoring. Note this should be the full test tree since it defines the local topic label
            models used for scoring.

        eval_ids : list of strings
            The ids of the tests to score.
        """
        
        # expand templates in the test tree
        eval_inputs = []
        eval_inds = []
        for i, id in enumerate(eval_ids):
            test = tests.loc[id]
            template_expansions = expand_template(test.input)
            for expansion in template_expansions:
                eval_inputs.append(expansion)
                eval_inds.append(i)
        # run the model
        try:
            model_out = self.model(sequences= eval_inputs,candidate_labels=self.label,hypothesis_template = self.hypothesis_template, multi_class=self.multi_class,th=self.th)
        except Exception as e:
            model_out = np.zeros((len(eval_inputs), len(self.model.output_names))) * np.nan # TODO: remove this hack after the user study
            log.error(e)
            log.error(eval_inputs)
            log.error("The model threw an exception when evaluating inputs! We are patching this disaster with np.nan for the sake of the user study!")
        # compute the output strings and probabilites for each output in template form
        out_strings = [[] for _ in range(len(eval_ids))]
        out_probs = [[] for _ in range(len(eval_ids))]
        i = 0
        while i < len(model_out):
            out_strings[eval_inds[i]].append(model_out[i]["labels"][np.argmax(model_out[i]["scores"])])
            out_probs[eval_inds[i]].append(model_out[i]["scores"])
            i += 1
        for i in set(eval_inds):
            out_strings[i] = "|".join(out_strings[i]) # template outputs are joined by |
            out_probs[i] = np.column_stack(out_probs[i]) # the probability of a set of items is the prob of the min item

        # compute the embeddings as a batch (this fills a cache we will use when scoring below)
        adatest.embed(list(tests.loc[eval_ids, "input"]))

        # score all the tests
        scores = []
        outputs = []
        for i, ind in enumerate(eval_inds):
            outputs.append(out_strings[ind])
            scores.append(self._score_test(tests, eval_ids[ind], out_probs[ind], self.top_probs))

        return outputs,scores
 
    def _score_test(self, tests, id, probs, top_probs):
        test = tests.loc[id]
        fail_prob = 0
        pass_prob = 0

        # if this is not a templated test
        if probs.shape[1] == 1:
            inds = np.argsort(probs[:,0])[::-1]
            for ind in inds[:top_probs]:

                # Scott: we could use any manually given labels when possible, but then that would make the score depend on the label 
                # and so we would either need to save the full output of the model or recompute every time
                # if self.model.output_names[ind] == test["output"] and test["labeler"] != "imputed":
                #     label = test["label"]
                
                # we use the local topic model to predict the label
                label = tests.topic_labeling_model(test.topic)(test.input, self.model.output_names[ind])
                if label == "fail":
                    fail_prob += probs[ind, 0]
                elif label == "pass":
                    pass_prob += probs[ind, 0]

            if not (fail_prob + pass_prob > 0):
                return np.nan
            else:
                return fail_prob / (pass_prob + fail_prob)
        else:
            raise NotImplementedError("TODO: implement classifer scoring for templated tests")