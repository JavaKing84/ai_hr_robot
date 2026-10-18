
import feedparser
import requests
from shared.config import NEWS_CONFIG

def fetch_news(rss_url):
    s = feedparser.parse(rss_url)

    if s.bozo:
        print("[警告] 抓取失败", rss_url)
        return []

    result_list = []
    for e in s.entries:
        one_news = {
            "title": e.get("title", ""),
            "link": e.get("link", ""),
        }
        result_list.append(one_news)

    return result_list

def score_news(item):
    score = 0
    title = item["title"].lower()

    for keyword in NEWS_CONFIG["important_keywords"]:
        if keyword.lower() in title:
            score += 10

    return score

def get_top_news():
    all_news = []

    for url in NEWS_CONFIG["rss_feeds"]:
        news_list = fetch_news(url)
        all_news.extend(news_list)

    scored = sorted(all_news, key=score_news, reverse=True)

    top_news = scored[:NEWS_CONFIG["top_n"]]

    return top_news

if __name__ == "__main__":
    print("正在抓取并筛选新闻...")
    result = get_top_news()

    print("重点新闻")
    for n in result:
        print(n["title"], n["link"])
