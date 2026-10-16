# Matrix 行动纲领

> 产品名为 **Matrix**（原名 Agent Network Demo）。正文中的“Agent Network”指网络这一概念，保留原文。
>
> 项目的方向性文档（原文存档）。工程层面的约束与工作方式见 [AGENTS.md](AGENTS.md)；
> 两者冲突时，以本文件的方向为准，再去修正 AGENTS.md 的具体做法。

## 一、核心目标

我们正在构建一个面向未来智能协作模式的 Agent 网络平台。

我们的目标不是创造一个固定的"主 Agent + 子 Agent"系统，而是探索一种更加开放的智能组织形态：

> **让任意 Agent 成为网络中的一个智能节点，通过能力暴露、权限授权、任务委托和知识协作，与其他 Agent 动态连接，形成可以持续运行和演化的 Agent 网络。**

在这个网络中：

- 一个 Agent 可以调用其他 Agent；
- 一个 Agent 可以授权其他 Agent 代表自己执行任务；
- 一个 Agent 可以同时服务多个上层 Agent；
- 一个执行 Agent 也可以成为另一个任务网络中的主 Agent；
- "主 Agent"不是系统定义的角色，而是当前目标关系中的根节点。

我们希望验证：

> 当 Agent 从"工具"升级为"可连接节点"后，是否能够形成比传统 Workflow 更灵活、更强大的智能协作系统。

---

# 二、核心理念

## 1. Agent 不是角色，而是节点

传统 Multi-Agent 系统通常定义：

- Research Agent
- Coding Agent
- Planning Agent
- Review Agent

这种方式本质仍然是固定流程。

我们的理念：

> Agent 是一个独立能力节点，而角色来自它当前承担的关系。

同一个 Agent 可以：

- 在一个任务中作为执行者；
- 在另一个任务中作为负责人；
- 在第三个网络中作为能力提供者。

例如：

```
                    Product Agent

                         |
                    委托研究

                         |

                  Research Agent

                    /          \

              Web Agent     Data Agent
```

Research Agent 既是 Product Agent 的执行节点，同时也是 Web Agent 和 Data Agent 的协调节点。

---

# 三、核心模型：Agent Network

系统不是构建树状组织，而是构建 Agent Graph。

```
                 Agent A


              /          \


          Agent B        Agent C


              |

          Agent D

```

网络中的基本元素：

## Agent Node

代表一个智能能力实体。

包含：

- 身份
- 能力
- 状态
- 记忆
- 执行环境
- 权限


## Relationship Edge

代表 Agent 之间的关系。

关系不是简单调用，而是：

> 授权与委托。


例如：

Agent A：

"我授权 Agent B 帮助我完成市场分析。"

这形成：

```
Agent A
   |
   | delegation
   |
Agent B
```

---

# 四、核心机制：Delegation（任务授权）

Agent 网络区别于普通 API 调用的关键：

不是：

```
A 调用 B
B 返回结果
```

而是：

```
A 授权 B

B 在授权范围内自主完成目标

B 返回结果与证据
```

一次授权包含：

## 目标

为什么执行这个任务。

## 权限

允许 Agent 做什么。

## 约束

不能做什么。

## 输出要求

需要产生什么结果。

示例：

```
Delegation:

From:
Product Agent

To:
Research Agent


Mission:
分析 AI Agent 市场趋势


Authority:
- 搜索公开信息
- 创建分析报告


Constraint:
- 不进行商业决策


Expected Outcome:
市场分析报告
```

---

# 五、核心原则：能力共享，状态隔离

Agent 网络最大的挑战不是调用，而是上下文管理。

当一个 Agent 同时被多个 Agent 使用：

```
          Product Agent
                |
                |
          Research Agent
                |
                |
        Investment Agent

```

必须避免任务之间相互污染。

因此：

## 默认隔离

每个任务拥有独立上下文。

## 显式共享

只有明确授权的信息才能传播。


Agent 状态分层：

```
Agent

├── Task Context
│      当前任务状态
│
├── Session Memory
│      当前合作关系
│
├── Agent Memory
│      Agent 自身经验
│
└── Shared Knowledge
       公共知识
```

---

# 六、Demo 验证目标

Demo 不追求模拟完整 Agent 社会，而验证最核心闭环：

## 一个目标如何驱动 Agent 网络形成。


用户输入：

```
我要研究一个新的 AI 产品机会。
```

系统：

1. 创建目标节点。

2. 主 Agent 分析目标需要哪些能力。

3. 发现当前能力不足。

4. 寻找或创建相关 Agent。

例如：

```
Goal Agent

    |

    +-- Market Research Agent

    |

    +-- Technical Analysis Agent

    |

    +-- Competitive Intelligence Agent

```

5. Agent 执行任务。

6. 结果返回。

7. 主 Agent 根据结果调整下一步行动。

---

# 七、Demo 第一阶段范围

## 必须实现

### 1. Agent Registry

管理 Agent 节点。

包括：

- 身份
- 能力描述
- 状态


### 2. Agent Discovery

根据目标寻找合适 Agent。

例如：

```
需要:
代码分析能力

寻找:

Coding Agent
```

---

### 3. Delegation Engine

实现：

- 委托
- 权限控制
- 任务生命周期


---

### 4. Context Management

实现：

- 任务隔离
- 信息共享
- 结果传播


---

### 5. Network Visualization

展示：

```
Agent Graph

关系

任务流

授权链
```

---

# 八、长期愿景

未来的 Agent 世界不会只有一个超级 Agent。

更可能是：

```
Human

  |

Personal Agent


  |

Agent Network


  |

Thousands of Specialized Agents

```

每个 Agent 都可以：

- 提供能力；
- 接受委托；
- 建立合作关系；
- 创建新的执行节点；
- 在更大的网络中承担不同角色。


最终形成：

> 一个由智能节点组成的开放式协作网络。

---

# 九、我们的探索问题

这个 Demo 不只是验证技术，而是在探索几个未来问题：

1. Agent 是否可以像互联网节点一样被发现和连接？

2. Agent 之间的授权关系是否可以形成可靠协作？

3. 动态 Agent 网络是否比固定 Workflow 更适合复杂任务？

4. 当 Agent 数量增长后，如何管理信任、权限和知识流动？

---

# 十、行动准则

我们不构建：

- 一个聊天机器人；
- 一个固定流程编排器；
- 一个简单的 Agent 调度平台。


我们探索：

> **下一代智能系统的组织基础设施。**

第一阶段目标：

让两个 Agent 能够可靠连接。

第二阶段目标：

让多个 Agent 能够形成动态协作网络。

第三阶段目标：

让 Agent 网络能够围绕目标自主组织和演化。

从一个简单 Demo 开始，验证 Agent 从工具走向网络节点的可能性。
