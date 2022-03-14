from setuptools import setup, find_packages

setup(
  name = 'ETSformer-pytorch',
  packages = find_packages(exclude=[]),
  version = '0.0.1',
  license='MIT',
  description = 'ETSTransformer - Exponential Smoothing Transformer for Time-Series Forecasting - Pytorch',
  author = 'Phil Wang',
  author_email = 'lucidrains@gmail.com',
  url = 'https://github.com/lucidrains/ETSformer-pytorch',
  keywords = [
    'artificial intelligence',
    'deep learning',
    'transformers',
    'attention-mechanism',
    'time-series',
    'forecasting'
  ],
  install_requires=[
    'einops>=0.4',
    'torch>=1.6',
  ],
  classifiers=[
    'Development Status :: 4 - Beta',
    'Intended Audience :: Developers',
    'Topic :: Scientific/Engineering :: Artificial Intelligence',
    'License :: OSI Approved :: MIT License',
    'Programming Language :: Python :: 3.6',
  ],
)