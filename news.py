import feedparser
import requests
import concurrent.futures
from io import BytesIO
import time
from datetime import datetime, timedelta

CONFIG = {
    "rss_feeds": [
        "https://www.solidot.org/index.rss",
        "https://sspai.com/feed"
    ],
    "important_keywords": ["AI", "LLM", "GPT", "chip", "robot", "大模型", "芯片", "苹果", "华为"],
    "top_n": 3
}

def fetch_news(rss_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(rss_url, headers=headers, timeout=5)
        resp.raise_for_status()
        
        feed = feedparser.parse(BytesIO(resp.content))
        if feed.bozo and not feed.entries:
            print(f"[警告] 解析异常: {rss_url}")
            return []
            
        news_list = []
        now = datetime.now()
        
        for e in feed.entries:
           
            published_time = None
            if hasattr(e, 'published_parsed') and e.published_parsed:
                published_time = datetime.fromtimestamp(time.mktime(e.published_parsed))
            elif hasattr(e, 'updated_parsed') and e.updated_parsed:
                published_time = datetime.fromtimestamp(time.mktime(e.updated_parsed))
            
            if published_time and (now - published_time).days > 3:
                continue

          
            news_list.append({
                "title": e.get("title", ""), 
                "link": e.get("link", ""),
                "time": published_time or datetime.min 
            })
            
        return news_list
    except Exception as e:
        print(f"[错误] 请求 {rss_url} 时发生异常: {e}")
        return []

def score_news(item):
 
    return sum(10 for kw in CONFIG["important_keywords"] if kw.lower() in item["title"].lower())

def get_daily_news():
    all_news = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CONFIG["rss_feeds"])) as executor:
        future_to_url = {executor.submit(fetch_news, url): url for url in CONFIG["rss_feeds"]}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                news_list = future.result()
                all_news.extend(news_list)
            except Exception as exc:
                print(f'{url} 抓取时产生异常: {exc}')
                
   
    scored = sorted(all_news, key=lambda x: (score_news(x), x["time"]), reverse=True)
    
    top_3 = scored[:CONFIG["top_n"]]
    return top_3

if __name__ == "__main__":
    start_time = time.time()
    news = get_daily_news()
    print(f"共耗时 {time.time() - start_time:.2f} 秒")
    for n in news:
        print(f"- {n['title']}")