from setuptools import setup, find_packages

setup(
    name="polyclaw",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "feedparser",
        "httpx",
        "rich",
        "pandas",
        "rapidfuzz",
    ],
    entry_points={
        "console_scripts": [
            "polyclaw=polymarket_news_edge.scanner:main",
        ],
    },
    python_requires=">=3.10",
)
