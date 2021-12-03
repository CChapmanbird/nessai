# -*- coding: utf-8 -*-
"""
Global configuration for nessai.
"""
LOGL_DTYPE = 'f8'
IT_DTYPE = 'i4'
DEFAULT_FLOAT_DTYPE = 'f8'
CORE_PARAMETERS = ['logP', 'logL', 'it']
DEFAULT_VALUES_CORE = [0.0, 0.0, 0]
EXTRA_PARAMETERS = []
DEFAULT_VALUES_EXTRA = []
NON_SAMPLING_PARAMETERS = CORE_PARAMETERS + EXTRA_PARAMETERS
DEFAULT_VALUES = DEFAULT_VALUES_CORE + DEFAULT_VALUES_EXTRA
