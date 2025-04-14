from bs4 import BeautifulSoup
import logging
import time
import random
import asyncio
import os
from pyppeteer import launch
from pyppeteer.errors import TimeoutError, NetworkError
import dotenv
from util.common_util import CommonUtil
from util.llm_util import LLMUtil
from util.oss_util import OSSUtil

# 加载环境变量
dotenv.load_dotenv()

llm = LLMUtil()
oss = OSSUtil()

# 设置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(filename)s - %(funcName)s - %(lineno)d - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 更丰富和现代的User-Agent列表
global_agent_headers = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux i686; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
]

class WebsitCrawler:
    def __init__(self):
        self.browser = None
        self.max_retries = 3
        self.retry_delay = 2
        self.page_load_timeout = 90000  # 增加页面加载超时时间到90秒
        self.cloudflare_wait = 15000   # Cloudflare检查等待时间

    async def init_browser(self):
        """初始化浏览器实例，如果已存在则先关闭"""
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.warning(f"关闭现有浏览器实例失败: {e}")
        
        try:
            self.browser = await launch(headless=True,
                                     ignoreDefaultArgs=["--enable-automation"],
                                     ignoreHTTPSErrors=True,
                                     args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                                           '--disable-software-rasterizer', '--disable-setuid-sandbox',
                                           '--disable-web-security', '--disable-features=IsolateOrigins,site-per-process',
                                           '--enable-features=NetworkService', 
                                           '--disable-blink-features=AutomationControlled'],
                                     handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False)
            logger.info("浏览器实例初始化成功")
            return self.browser
        except Exception as e:
            logger.error(f"浏览器实例初始化失败: {e}")
            self.browser = None
            raise

    async def create_page(self):
        """创建新的页面，并进行基本设置"""
        if not self.browser:
            await self.init_browser()
            
        page = await self.browser.newPage()
        
        # 设置更多的浏览器仿真参数，绕过反爬虫检测
        await page.evaluateOnNewDocument("""
        () => {
            // 覆盖navigator属性,使其更难被检测为自动化浏览器
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
            
            // 覆盖window.chrome
            window.chrome = {
                runtime: {},
            };
            
            // 创建plugins
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            
            // 覆盖语言,使用更普遍的语言设置
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
        }
        """)
            
        # 设置用户代理
        user_agent = random.choice(global_agent_headers)
        await page.setUserAgent(user_agent)
        logger.info(f"设置User-Agent: {user_agent}")
        
        # 设置页面视口大小
        width = 1920  # 默认宽度为 1920
        height = 1080  # 默认高度为 1080
        await page.setViewport({'width': width, 'height': height})
        
        # 设置cookies (如果需要)
        # await page.setCookie(...)
        
        # 请求拦截设置
        await page.setRequestInterception(True)
        
        def intercept_request(request):
            # 在加载阶段，先允许所有请求通过，确保反爬机制能正常工作
            # 仅阻止不必要的资源
            if request.resourceType in ['image', 'media', 'font'] and 'cloudflare' not in request.url:
                asyncio.ensure_future(request.abort())
            else:
                asyncio.ensure_future(request.continue_({
                    'headers': {
                        **request.headers,
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Cache-Control': 'no-cache',
                        'Pragma': 'no-cache',
                        'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="123", "Chromium";v="123"',
                        'Sec-Ch-Ua-Mobile': '?0',
                        'Sec-Ch-Ua-Platform': '"Windows"',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                        'Upgrade-Insecure-Requests': '1',
                    }
                }))
                
        page.on('request', intercept_request)
        
        return page, width, height

    async def handle_cloudflare(self, page, url):
        """专门处理Cloudflare验证"""
        logger.info("检测到Cloudflare保护，尝试绕过...")
        
        # 检查是否有Cloudflare挑战
        cloudflare_detected = False
        try:
            # 检查特征文本
            content = await page.content()
            cloudflare_detected = "Just a moment" in content or "Checking your browser" in content
        except:
            pass
            
        if cloudflare_detected:
            logger.info("发现Cloudflare验证页面，等待验证通过...")
            
            # 关闭请求拦截以确保页面正常加载
            try:
                await page.setRequestInterception(False)
            except:
                pass
                
            # 等待Cloudflare检查完成
            try:
                # 等待较长时间，让Cloudflare验证完成
                await asyncio.sleep(random.uniform(5, 8))
                
                # 检查是否有"点击我"按钮或其他互动元素
                try:
                    # 尝试查找并点击可能的按钮
                    elements = await page.querySelectorAll('form input[type="submit"]')
                    if elements:
                        for element in elements:
                            await element.click()
                            await asyncio.sleep(3)
                except:
                    pass
                    
                # 使用等待直到导航来确保验证后的页面加载
                await page.waitForNavigation({
                    'waitUntil': 'networkidle0',
                    'timeout': self.cloudflare_wait
                })
                
                # 最后再等待一点时间确保页面完全加载
                await asyncio.sleep(3)
                
                logger.info("Cloudflare验证成功通过")
                return True
            except Exception as e:
                logger.warning(f"等待Cloudflare验证超时: {e}")
                return False
        else:
            logger.info("未检测到Cloudflare验证")
            return True

    async def safe_page_goto(self, page, url, attempt=1):
        """安全的页面访问，包含重试逻辑和Cloudflare处理"""
        try:
            logger.info(f"尝试访问页面 {url}，第{attempt}次尝试")
            
            # 设置超时但让页面继续加载
            navigation_attempt = asyncio.create_task(
                page.goto(url, {
                    'timeout': self.page_load_timeout, 
                    'waitUntil': ['domcontentloaded']
                })
            )
            
            try:
                response = await navigation_attempt
            except TimeoutError:
                logger.warning(f"页面加载超时 {url}，但继续处理")
                response = None
                
            # 检查是否需要处理Cloudflare
            await self.handle_cloudflare(page, url)
            
            # 再次等待页面完全加载
            try:
                await page.waitForNavigation({
                    'waitUntil': 'networkidle2',
                    'timeout': 5000
                })
            except:
                # 忽略额外等待的超时
                pass
                
            return response
        except NetworkError as e:
            logger.error(f"网络错误 {url}，第{attempt}次尝试: {e}")
            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_delay * attempt)  # 指数退避
                return await self.safe_page_goto(page, url, attempt + 1)
            return None
        except Exception as e:
            logger.error(f"页面访问异常 {url}，第{attempt}次尝试: {e}")
            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_delay * attempt)
                return await self.safe_page_goto(page, url, attempt + 1)
            return None

    async def save_error_snapshot(self, page, url, error_msg):
        """保存出错页面的快照和HTML内容，便于调试"""
        try:
            domain = CommonUtil.get_name_by_url(url)
            timestamp = int(time.time())
            
            # 创建错误日志目录
            error_dir = './error_logs'
            if not os.path.exists(error_dir):
                os.makedirs(error_dir)
                
            # 保存屏幕截图
            screenshot_path = f'{error_dir}/error_{domain}_{timestamp}.png'
            await page.screenshot({'path': screenshot_path})
            
            # 保存HTML内容
            html = await page.content()
            with open(f'{error_dir}/error_{domain}_{timestamp}.html', 'w', encoding='utf-8') as f:
                f.write(html)
                
            logger.info(f"已保存错误页面快照: {screenshot_path}")
            
        except Exception as e:
            logger.error(f"保存错误快照失败: {e}")

    # 爬取指定URL网页内容
    async def scrape_website(self, url, tags, languages):
        """使用优化的方式爬取网站内容"""
        page = None
        start_time = int(time.time())
        screenshot_path = None
        
        try:
            logger.info(f"开始处理网站: {url}")
            
            # 确保URL格式正确
            if not url.startswith('http://') and not url.startswith('https://'):
                url = 'https://' + url
                
            # 每次爬取前重新初始化浏览器，避免连接问题
            if not self.browser:
                await self.init_browser()
                
            # 创建并配置页面
            page, width, height = await self.create_page()
            
            # 访问页面
            await self.safe_page_goto(page, url)
            
            # 检查页面标题，如果包含防爬特征，等待更长时间
            title = await page.title()
            if title and ("Just a moment" in title or "验证" in title or "Checking" in title):
                logger.info(f"检测到防爬验证页面，标题: {title}, 等待更长时间...")
                await asyncio.sleep(10)  # 等待更长时间
                
                # 关闭请求拦截，允许所有资源加载
                try:
                    await page.setRequestInterception(False)
                except:
                    pass
                    
                # 尝试刷新页面
                await page.reload({'waitUntil': ['domcontentloaded', 'networkidle2'], 'timeout': 60000})
                await asyncio.sleep(5)
                
                # 重新检查标题
                title = await page.title()
                if "Just a moment" in title or "验证" in title or "Checking" in title:
                    logger.warning(f"仍然在验证页面，尝试最后的绕过方法")
                    # 尝试模拟用户行为，移动鼠标和滚动页面
                    await page.mouse.move(100, 100)
                    await page.mouse.down()
                    await page.mouse.move(200, 200)
                    await page.mouse.up()
                    
                    # 随机滚动页面
                    for _ in range(3):
                        await page.evaluate(f"""
                            window.scrollTo(0, {random.randint(100, 500)});
                        """)
                        await asyncio.sleep(1)
                        
                    await asyncio.sleep(10)
            
            # 等待页面加载完成，增加额外等待时间
            await asyncio.sleep(5)
            
            # 获取网页内容
            origin_content = await page.content()
            soup = BeautifulSoup(origin_content, 'html.parser')

            # 通过标签名提取内容
            title = await page.title()
            logger.info(f"页面标题: {title}")
            
            # 如果标题仍然包含防爬特征，尝试解析更多内容
            if "Just a moment" in title or "验证" in title or "Checking" in title:
                # 使用更高级的方法尝试获取网页内容
                try:
                    # 强制执行JavaScript来获取页面内容
                    real_content = await page.evaluate("""
                        () => {
                            // 等待几秒钟，让Cloudflare验证完成
                            return new Promise((resolve) => {
                                setTimeout(() => {
                                    resolve(document.documentElement.outerHTML);
                                }, 5000);
                            });
                        }
                    """)
                    
                    # 再次解析内容
                    soup = BeautifulSoup(real_content, 'html.parser')
                    # 尝试重新获取标题
                    title_element = soup.find('title')
                    if title_element and title_element.string:
                        title = title_element.string.strip()
                        logger.info(f"JavaScript获取的标题: {title}")
                except Exception as e:
                    logger.error(f"尝试用JavaScript获取内容失败: {e}")
            
            # 根据url提取域名生成name
            name = CommonUtil.get_name_by_url(url)

            # 获取网页描述
            description = ''
            meta_description = soup.find('meta', attrs={'name': 'description'})
            if meta_description and meta_description.get('content'):
                description = meta_description['content'].strip()

            if not description:
                meta_description = soup.find('meta', attrs={'property': 'og:description'})
                if meta_description and meta_description.get('content'):
                    description = meta_description['content'].strip()
                    
            # 如果仍然无法获取描述，尝试从页面内容提取
            if not description:
                try:
                    # 提取页面中的第一段文本作为描述
                    paragraphs = soup.find_all('p')
                    for p in paragraphs:
                        text = p.get_text().strip()
                        if text and len(text) > 50:  # 只取有实质内容的段落
                            description = text[:250]  # 限制长度
                            break
                except:
                    pass

            logger.info(f"提取基本信息: url:{url}, title:{title}, description:{description[:50] if description else ''}...")

            # 关闭请求拦截以准备截图
            try:
                await page.setRequestInterception(False)
            except:
                pass
            
            # 重新加载页面以获取完整渲染（用于截图）
            try:
                await page.reload({'timeout': 30000, 'waitUntil': ['load', 'networkidle2']})
                await asyncio.sleep(2)  # 给页面充分渲染的时间
            except Exception as e:
                logger.warning(f"重新加载页面失败，继续使用当前页面: {e}")
            
            # 生成网站截图
            image_key = oss.get_default_file_key(url)
            dimensions = await page.evaluate(f'''() => {{
                return {{
                    width: document.documentElement.clientWidth || {width},
                    height: document.documentElement.clientHeight || {height},
                    deviceScaleFactor: window.devicePixelRatio || 1
                }};
            }}''')
            
            # 截屏并设置图片大小
            try:
                safe_name = url.replace("https://", "").replace("http://", "").replace("/", "").replace(".", "-")
                screenshot_path = f'./{safe_name}.png'
                
                # 尝试多次截图，确保成功
                screenshot_success = False
                for attempt in range(self.max_retries):
                    try:
                        # 使用全页面截图而不是裁剪
                        await page.screenshot({
                            'path': screenshot_path,
                            'fullPage': False,
                            'clip': {
                                'x': 0,
                                'y': 0,
                                'width': min(dimensions['width'], 1920),
                                'height': min(dimensions['height'], 1080)
                            }
                        })
                        screenshot_success = True
                        logger.info(f"截图成功: {screenshot_path}")
                        break
                    except Exception as e:
                        logger.warning(f"截图失败，第{attempt+1}次尝试: {e}")
                        await asyncio.sleep(1)
                
                if not screenshot_success:
                    logger.warning("所有截图尝试均失败，尝试使用简化方法")
                    # 尝试最简单的截图方法
                    await page.screenshot({'path': screenshot_path})
                
                # 上传图片，返回图片地址
                screenshot_key = oss.upload_file_to_r2(screenshot_path, image_key)
                
                # 生成缩略图
                thumnbail_key = oss.generate_thumbnail_image(url, image_key)
            except Exception as e:
                logger.error(f"截图或上传过程失败: {e}")
                # 如果截图失败，提供一个空值
                screenshot_key = None
                thumnbail_key = None

            # 抓取整个网页内容
            content = soup.get_text()
            
            # 检查是否成功获取了有实质内容的文本
            if len(content.strip()) < 100 and "Just a moment" in content:
                logger.warning("获取的内容可能不完整，尝试更高级的方法获取内容")
                try:
                    # 使用JavaScript分析页面，获取所有可见文本
                    js_content = await page.evaluate("""
                        () => {
                            function getVisibleText(element) {
                                let text = '';
                                
                                // 忽略隐藏元素
                                const style = window.getComputedStyle(element);
                                if (style.display === 'none' || style.visibility === 'hidden') {
                                    return '';
                                }
                                
                                // 处理文本节点
                                for (let node of element.childNodes) {
                                    if (node.nodeType === Node.TEXT_NODE) {
                                        text += node.textContent.trim() + ' ';
                                    } else if (node.nodeType === Node.ELEMENT_NODE) {
                                        // 忽略脚本和样式标签
                                        const tagName = node.tagName.toLowerCase();
                                        if (tagName !== 'script' && tagName !== 'style') {
                                            text += getVisibleText(node) + ' ';
                                        }
                                    }
                                }
                                return text;
                            }
                            
                            return getVisibleText(document.body);
                        }
                    """)
                    
                    if js_content and len(js_content.strip()) > 100:
                        content = js_content
                        logger.info("成功使用JavaScript获取页面内容")
                except Exception as e:
                    logger.error(f"使用JavaScript获取内容失败: {e}")

            # 使用llm工具处理content
            detail = llm.process_detail(content)
            
            # 如果tags为非空数组，则使用llm工具处理tags
            processed_tags = None
            if tags and detail:
                processed_tags = llm.process_tags('tag_list is:' + ','.join(tags) + '. content is: ' + detail)

            # 循环languages数组， 使用llm工具生成各种语言
            processed_languages = []
            if languages:
                for language in languages:
                    logger.info(f"正在处理 {url} 站点，生成 {language} 语言")
                    processed_title = llm.process_language(language, title)
                    processed_description = llm.process_language(language, description)
                    processed_detail = llm.process_language(language, detail)
                    processed_languages.append({'language': language, 'title': processed_title,
                                                'description': processed_description, 'detail': processed_detail})

            logger.info(f"{url} 站点处理成功")
            
            # 关闭页面
            if page:
                await page.close()
                
            # 处理图片URL，添加域名前缀
            custom_domain = os.getenv('S3_CUSTOM_DOMAIN')
            if screenshot_key and not screenshot_key.startswith('http'):
                if custom_domain:
                    screenshot_key = f"https://{custom_domain}{screenshot_key}"
                else:
                    # 如果没有自定义域名，使用默认S3端点
                    s3_endpoint = os.getenv('S3_ENDPOINT_URL')
                    s3_bucket = os.getenv('S3_BUCKET_NAME')
                    screenshot_key = f"{s3_endpoint}/{s3_bucket}{screenshot_key}"
            
            if thumnbail_key and not thumnbail_key.startswith('http'):
                if custom_domain:
                    thumnbail_key = f"https://{custom_domain}{thumnbail_key}"
                else:
                    # 如果没有自定义域名，使用默认S3端点
                    s3_endpoint = os.getenv('S3_ENDPOINT_URL')
                    s3_bucket = os.getenv('S3_BUCKET_NAME')
                    thumnbail_key = f"{s3_endpoint}/{s3_bucket}{thumnbail_key}"
                
            return {
                'name': name,
                'url': url,
                'title': title,
                'description': description,
                'detail': detail,
                'screenshot_data': screenshot_key,
                'screenshot_thumbnail_data': thumnbail_key,
                'tags': processed_tags,
                'languages': processed_languages,
            }
        except Exception as e:
            logger.error(f"处理 {url} 站点异常，错误信息: {str(e)}")
            
            # 保存错误页面快照
            if page:
                await self.save_error_snapshot(page, url, str(e))
                
            # 尝试重启浏览器
            try:
                await self.init_browser()
            except:
                pass
                
            return None
        finally:
            # 清理资源
            try:
                if page:
                    await page.close()
            except:
                pass
                
            # 清理临时文件
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except:
                    pass
                    
            # 计算程序执行时间
            execution_time = int(time.time()) - start_time
            logger.info(f"处理 {url} 用时: {execution_time} 秒")

    async def close(self):
        """关闭浏览器实例，释放资源"""
        if self.browser:
            try:
                await self.browser.close()
                self.browser = None
                logger.info("浏览器实例已关闭")
            except Exception as e:
                logger.error(f"关闭浏览器失败: {e}")
