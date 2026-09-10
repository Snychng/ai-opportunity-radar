<p align="center">
  <img src="assets/aor-wordmark.svg" alt="AOR 青蓝渐变像素字标" width="880">
</p>

<h1 align="center">AI Opportunity Radar</h1>

<p align="center">找到值得自己做的创业项目</p>

---

AOR 是面向 AI Agent 的创业机会研究 Skill，支持每日雷达、定向扫描、机会深挖与历史回顾。从收费产品、真实需求和地区差异出发，整理可追溯的证据，筛选候选，再结合你的能力、时间和预算，找到值得亲自验证的方向。

Agent 负责核验原文、分析机会与提出行动；Python CLI 负责采集、证据整理、候选分层、评分和报告。研究可中断恢复，历史证据可复用，报告与验证记录保存在本地。

## Skill 执行流程

```mermaid
flowchart TD
    A[确定研究主题与个人约束] --> B[规划检索 · 读取历史 · 采集信源]
    B --> C[Agent 核验原文、收费对标与反证]
    C --> D[扩展候选 · 过滤分层 · 明确证据缺口]
    D -. 按需补证 .-> B
    D --> E[Agent 提供评分依据与行动判断]
    E --> F[生成报告 · 校验并保存本地状态]
    F --> G[选择项目 · 设计实验 · 记录验证结果]

    classDef agent fill:#E8F7F6,stroke:#16857C,color:#164B46
    classDef engine fill:#EDF3FC,stroke:#5B82BE,color:#263E61
    classDef action fill:#F5F5F4,stroke:#A8A29E,color:#44403C
    class C,E agent
    class B,D,F engine
    class A,G action
```

研究结论是待验证的候选；客户实验的真实反馈，决定是否继续投入。

<p align="center">
  <a href="references/quick-start.md">安装与快速开始</a> ·
  <a href="SKILL.md">Skill 入口</a> ·
  <a href="references/source-catalog.md">信源目录</a> ·
  <a href="references/engine-architecture.md">架构说明</a>
</p>
