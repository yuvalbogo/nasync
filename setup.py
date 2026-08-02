from setuptools import setup, find_packages

setup(
    name="samba-sync-auto",
    version="0.1.0",
    description="Automatic SAMBA-based sync tool for home NAS backup",
    author="Yuval Bogo",
    author_email="yuvalbogo2@gmail.com",
    packages=find_packages(),
    install_requires=[
        "click==8.1.7",
        "watchdog==3.0.0",
        "appdirs==1.4.4",
    ],
    entry_points={
        "console_scripts": [
            "samba-sync=samba_sync.cli:main",
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS",
    ],
)
