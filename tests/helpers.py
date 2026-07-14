from __future__ import annotations


def opportunity_block(
    index: int,
    *,
    sensitive: str = "无",
    platform: str = "否",
    competition: str = "中",
    include_second_source: bool = True,
    confidence: int = 7,
) -> str:
    opportunity_id = f"OPP-20260714-A1B2{index:02X}"
    platform_section = "\n#### 单边切入口\n先提供单边需求定义工具。\n" if platform == "是" else ""
    new_form_section = "\n#### 老产品的新形态\n把目录产品改造成主动匹配代理。\n" if competition == "高" else ""
    second_evidence = f"""
##### 证据 2

- 原文：This still takes me several hours every week.
- 中文翻译：这件事每周仍然要花我几个小时。
- 来源：https://community.example.org/thread/{index}
- 发布时间：2026-07-12
- 日期置信度：高
- 采集时间：2026-07-14T09:10:00+08:00
- 语言：英语
- 访问方式：原生平台
- 互动量：18 个赞
""" if include_second_source else ""
    return f"""### {opportunity_id}｜测试机会 {index}

- 类型：老产品新形态
- 综合分：78
- 首笔收入潜力：8
- 长期规模潜力：8
- 个人影响力潜力：6
- 证据置信度：{confidence}
- 目标市场：全球
- 目标用户：小微企业
- 敏感领域：{sensitive}
- 数字化交付：是
- 一个月 MVP：是
- 平台型：{platform}
- 竞争强度：{competition}
- 数据获取：公开

#### 一句话产品

一个足够垂直的测试产品。

#### 用户场景与具体触发时刻

用户在重复完成数字任务时触发。

#### 原始证据

##### 证据 1

- 原文：I wish this workflow were automatic.
- 中文翻译：我希望这个流程能够自动完成。
- 来源：https://example.com/post/{index}
- 发布时间：2026-07-13
- 日期置信度：高
- 采集时间：2026-07-14T09:00:00+08:00
- 语言：英语
- 访问方式：搜索索引
- 互动量：42 条评论
{second_evidence}

#### 当前替代方案

手工复制和整理。

#### 需求规模与升温原因

近期出现多条相似讨论。
{new_form_section}
#### 现有竞品和集中差评

现有产品流程复杂。

#### 一个月 MVP

使用 Web SaaS 完成单任务闭环。

#### 首笔收入路径

直接联系十名目标用户。

#### 长期规模化路径

逐步转成订阅产品。

#### 获客与自然传播机制

通过目标社区和分享结果获客。

#### 数据来源及产品化可持续性

仅依赖公开或授权数据。
{platform_section}
#### 最大反对理由

需求可能不够高频。

#### 72 小时验证实验

制作交互原型并访谈五名用户。

#### AI 判断

建议继续验证，最大不确定性是付费意愿。
"""


def valid_report(count: int = 3, *, low_count_reason: bool = False) -> str:
    reason = "\n- 少于 3 个的原因：当日证据不足，不凑数。" if low_count_reason else ""
    blocks = "\n".join(opportunity_block(i + 1) for i in range(count))
    return f"""# AI 创业机会雷达日报｜2026-07-14

## 今日结论

- 深度机会数量：{count}{reason}

## 数据源覆盖

### 已覆盖

- Reddit：成功

### 跳过或失败

- TikTok：未登录，本次未宣称覆盖

## 采集费用

- TikHub 请求次数：17
- TikHub 预计费用 USD：0.053000
- TikHub 预计费用 RMB：0.3816
- TikHub 免费额度适用成本 USD：0.004000
- TikHub 免费额度不适用成本 USD：0.049000
- TikHub 实际账单：未执行；执行后以 TikHub 使用日志为准

## 综合推荐排名

1. 测试机会

## 分类视图

- 老产品新形态

## 深度机会

{blocks}

## 早期观察池

- 一个早期信号

## 历史变化

- 首次运行，无历史变化

## 方法与局限

- 本报告没有把访问失败的平台计入覆盖范围。
"""
