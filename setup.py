from setuptools import setup, find_packages

setup(
    name="netlapse",
    version="0.2.0",
    description="Network state time-lapse. Structured CLI state backup with versioned diffs.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Scott Peterman",
    license="GPL-3.0",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "fastapi>=0.110",
        "uvicorn[standard]>=0.27",
        "paramiko>=3.4",
        "textfsm>=1.1",
        "cryptography>=42.0",
        "gitpython>=3.1",
        "pyyaml>=6.0",
        "deepdiff>=7.0",
    ],
    extras_require={
        "dev": [
            "pytest",
            "httpx",      # for async test client
            "ruff",
        ],
    },
    entry_points={
        "console_scripts": [
            "netlapse=netlapse.app:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
        "Topic :: System :: Networking :: Monitoring",
    ],
)
