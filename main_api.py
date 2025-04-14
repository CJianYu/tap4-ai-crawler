import logging
import os
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import traceback
from concurrent.futures import ThreadPoolExecutor

from website_crawler import WebsitCrawler

app = FastAPI()
website_crawler = WebsitCrawler()
load_dotenv()
system_auth_secret = os.getenv('AUTH_SECRET')

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 设置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(filename)s - %(funcName)s - %(lineno)d - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 全局任务队列和任务状态字典
task_queue = asyncio.Queue()
task_status = {}
MAX_CONCURRENT_TASKS = 5  # 最大并发任务数
worker_tasks = []


class URLRequest(BaseModel):
    url: str
    tags: Optional[List[str]] = None
    languages: Optional[List[str]] = None


class AsyncURLRequest(URLRequest):
    callback_url: str
    key: str


@app.on_event("startup")
async def startup_event():
    """服务启动时初始化工作任务和浏览器实例"""
    logger.info("服务启动，初始化爬虫...")
    
    # 启动工作线程池
    for i in range(MAX_CONCURRENT_TASKS):
        task = asyncio.create_task(worker(i))
        worker_tasks.append(task)
    
    logger.info(f"已启动 {MAX_CONCURRENT_TASKS} 个工作任务")


@app.on_event("shutdown")
async def shutdown_event():
    """服务关闭时清理资源"""
    logger.info("服务关闭，清理资源...")
    
    # 关闭工作线程池
    for task in worker_tasks:
        task.cancel()
    
    # 关闭浏览器
    await website_crawler.close()
    
    logger.info("资源清理完成")


async def worker(worker_id):
    """工作线程函数，从队列获取任务并执行"""
    logger.info(f"工作线程 {worker_id} 已启动")
    
    while True:
        try:
            # 从队列获取任务
            task_id, url, tags, languages, callback_url, key = await task_queue.get()
            
            logger.info(f"工作线程 {worker_id} 开始处理任务 {task_id}: {url}")
            task_status[task_id] = "processing"
            
            # 执行爬虫任务
            try:
                result = await website_crawler.scrape_website(url.strip(), tags, languages)
                task_status[task_id] = "completed"
                
                # 如果有回调URL，发送结果
                if callback_url:
                    try:
                        logger.info(f'回调开始: {callback_url}')
                        response = requests.post(callback_url, json=result, headers={'Authorization': 'Bearer ' + key})
                        if response.status_code != 200:
                            logger.error(f'回调错误: {callback_url}, {response.text}')
                        else:
                            logger.info(f'回调成功: {callback_url}')
                    except Exception as e:
                        logger.error(f'回调异常: {callback_url}, {str(e)}')
                        logger.error(traceback.format_exc())
            except Exception as e:
                logger.error(f'任务 {task_id} 处理失败: {str(e)}')
                logger.error(traceback.format_exc())
                task_status[task_id] = "failed"
            
            # 标记任务完成
            task_queue.task_done()
            logger.info(f"工作线程 {worker_id} 完成任务 {task_id}")
        
        except asyncio.CancelledError:
            logger.info(f"工作线程 {worker_id} 被取消")
            break
        except Exception as e:
            logger.error(f"工作线程 {worker_id} 异常: {str(e)}")
            logger.error(traceback.format_exc())


@app.post('/site/crawl')
async def scrape(request: URLRequest, authorization: Optional[str] = Header(None)):
    """同步爬取网站内容"""
    url = request.url
    tags = request.tags  # tag数组
    languages = request.languages  # 需要翻译的多语言列表

    if system_auth_secret:
        # 配置了非空的auth_secret，才验证
        validate_authorization(authorization)

    try:
        result = await website_crawler.scrape_website(url.strip(), tags, languages)

        # 若result为None,则 code="10001"，msg="处理异常，请稍后重试"
        code = 200
        msg = 'success'
        if result is None:
            code = 10001
            msg = 'fail'

        # 将数据映射到 'data' 键下
        response = {
            'code': code,
            'msg': msg,
            'data': result
        }
        return response
    except Exception as e:
        logger.error(f"处理 {url} 失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'code': 10001,
            'msg': f'处理失败: {str(e)}',
            'data': None
        }


@app.post('/site/crawl_async')
async def scrape_async(background_tasks: BackgroundTasks, request: AsyncURLRequest,
                       authorization: Optional[str] = Header(None)):
    """异步爬取网站内容"""
    url = request.url
    callback_url = request.callback_url
    key = request.key  # 请求回调接口，放header Authorization: 'Bear key'
    tags = request.tags  # tag数组
    languages = request.languages  # 需要翻译的多语言列表

    if system_auth_secret:
        # 配置了非空的auth_secret，才验证
        validate_authorization(authorization)

    # 生成任务ID
    task_id = f"{url}-{int(asyncio.get_event_loop().time())}"
    
    # 将任务加入队列
    await task_queue.put((task_id, url.strip(), tags, languages, callback_url, key))
    task_status[task_id] = "queued"
    
    logger.info(f"任务 {task_id} 已加入队列，当前队列长度: {task_queue.qsize()}")

    # 返回任务ID
    return {
        'code': 200,
        'msg': 'success',
        'data': {
            'task_id': task_id,
            'status': 'queued'
        }
    }


@app.get('/site/task/{task_id}')
async def get_task_status(task_id: str, authorization: Optional[str] = Header(None)):
    """获取任务状态"""
    if system_auth_secret:
        validate_authorization(authorization)
        
    if task_id in task_status:
        return {
            'code': 200,
            'msg': 'success',
            'data': {
                'task_id': task_id,
                'status': task_status[task_id]
            }
        }
    else:
        return {
            'code': 404,
            'msg': '任务不存在',
            'data': None
        }


@app.get('/site/queue_status')
async def get_queue_status(authorization: Optional[str] = Header(None)):
    """获取队列状态"""
    if system_auth_secret:
        validate_authorization(authorization)
        
    return {
        'code': 200,
        'msg': 'success',
        'data': {
            'queue_size': task_queue.qsize(),
            'active_tasks': len([status for status in task_status.values() if status == "processing"]),
            'total_tasks': len(task_status)
        }
    }


def validate_authorization(authorization):
    """验证授权头"""
    if not authorization:
        raise HTTPException(status_code=400, detail="Missing Authorization header")
    if 'Bearer ' + system_auth_secret != authorization:
        raise HTTPException(status_code=401, detail="Authorization is error")


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8040)
