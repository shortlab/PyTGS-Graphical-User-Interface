from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="PyTGS",
    version="1.0.0",
    author="Akarsh Aurora and Angus Wylie",
    author_email="",
    description="Transient Grating Spectroscopy Analyzer - GUI Application",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/shortlab/PyTGS",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "pytgs=tgs_gui:main",
        ],
    },
    include_package_data=True,
    package_data={
        '': ['*.yaml'],
    },
)