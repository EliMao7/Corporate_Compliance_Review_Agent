"""
Regulation Crawler Module
采集公开法规数据（国家法律法规数据库 / 中国政府网政策文件库），为合规知识图谱提供语料
"""

from .regulation_crawler import RegulationCrawler, RobotsChecker

__all__ = ["RegulationCrawler", "RobotsChecker"]
