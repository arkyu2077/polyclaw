from setuptools import setup, find_packages

setup(
    name="polymarket-news-edge",
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
            "polymarket-edge=polymarket_news_edge.scanner:main",
        ],
    },
    python_requires=">=3.10",
)
