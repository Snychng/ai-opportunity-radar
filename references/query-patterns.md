# 查询模式

## 目录

1. 查询生成原则
2. 痛点与欲望模式
3. 商业与产品模式
4. 地区与多语言
5. 反证查询

## 1. 查询生成原则

从普通用户表达出发，不要求查询中出现 AI。组合“人群 + 场景 + 行为信号”，避免搜索宽泛的“AI startup ideas”。

每条查询只负责一个意图。使用多组窄查询替代一个包含所有关键词的超长查询。

不要把日期词硬塞进平台查询；使用工具的时间过滤。保留品牌、产品和人名的精确写法。

## 2. 痛点与欲望模式

### 英语基础模式

```text
"I wish there was" OR "someone should build"
"is there an app" OR "need a tool"
"still doing this manually" OR "takes hours every week"
"too expensive" OR "looking for an alternative"
"missing integration" OR "doesn't support"
"I use a spreadsheet" OR "copy and paste"
"I would pay for" OR "happy to pay"
"how do you handle" OR "what do you use for"
```

### 中文基础模式

```text
有没有工具可以
为什么还没有
只能手工处理
每周都要花几个小时
太贵了 有什么替代
不支持中文或国内平台
一直用表格凑合
愿意付费
大家怎么解决
求推荐 软件 插件 小程序
```

### 欲望和玩法

搜索：

- 用户主动分享或模仿的新玩法
- 身份表达、关系、陪伴、娱乐和创作欲望
- “希望产品能记住我、理解我、和朋友一起使用”
- 用户把现有产品用于设计之外的场景
- 评论区集中要求的新体验

## 3. 商业与产品模式

### 付费行为

```text
paid tool / subscription / cancelled / refund
hiring / freelancer / outsource / consultant
pricing too high / cheaper alternative
付费软件 / 取消订阅 / 外包 / 招人 / 找服务商
```

### 老产品新形态

将成熟品类分别与这些词组合：

```text
agent / match / personalized / voice / camera / memory
multiplayer / social / interactive / generated / embedded
代理 / 主动匹配 / 个性化 / 语音 / 拍照 / 长期记忆 / 社交 / 动态生成
```

搜索用户行为，不只搜索产品发布新闻。

### 产品评论

围绕：

- 反复出现的缺失功能
- 价格和套餐不满
- 复杂配置
- 移动端和语言问题
- 导入导出与集成失败
- 用户为绕过限制形成的工作流

## 4. 地区与多语言

每日固定使用英语和中文，再根据查询计划轮换地区语言。先让模型生成本地自然表达，再用回译检查含义，不要机械翻译英语短语。

查询计划必须实际携带本地语言字符串，不能只写语言标签或把国家名追加到英语查询后。每个地区维护多种本地表达并按日期确定性轮换；同一天重跑得到相同查询。社交平台同时采用“核心源 + 三日滚动源”，避免每天对全部平台重复发送相同关键词。

重点语言：

- 东南亚：印尼语、越南语、泰语、菲律宾语、马来语
- 非洲：英语、法语、葡萄牙语、南非荷兰语、斯瓦希里语、阿拉伯语
- 南亚：英语、印地语、乌尔都语、孟加拉语
- 中东：阿拉伯语、土耳其语、英语
- 拉美：西班牙语、葡萄牙语

地区查询至少加入一个本地行为或渠道，而不是只加入国家名。保留原文和中文翻译。

示例意图（运行时仍应使用计划中的已审核表达）：

```text
印尼语：aplikasi AI terlalu mahal masih dikerjakan manual butuh alat
泰语：เครื่องมือ AI แพงเกินไป ยังต้องทำเอง อยากได้แอป
阿拉伯语：أداة ذكاء اصطناعي غالية لا تدعم العربية عمل يدوي
西班牙语：herramienta IA demasiado cara todavía trabajo manual necesito app
```

## 5. 反证查询

对每个候选生成至少两类反证查询：

```text
[产品类别] failed startup / shut down / discontinued
[现有竞品] complaints / refund / pricing / alternatives
[需求] free workaround / open source
[新功能] built into [major platform]
[地区] local competitor / local language app
```

中文对应搜索失败项目、停止维护、退款、替代品、免费方案、平台内置和本地竞品。
