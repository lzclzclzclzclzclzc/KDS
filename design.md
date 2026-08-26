# KDS 侃大山
多个llm根据预设背景和system prompt无限制聊天

## 前端（简洁美观，浅色）
1. 开始页
- 配置页入口
- 历史对话
2. 配置页
- 可以浏览，导入历史配置并编辑
3. 聊天页：类似微信，纵向罗列消息

## 配置
- 人数
- 每个人自己的system prompt（可设置对某个人/某些人/全体可见）
- 共享的背景
- 每个人发言max_token（需要在system prompt里限制，但效果不会太好，因为llm无法计划输出多少token，超长则截断）
- 总输出max_token（本次对话里消耗的一切token）
- 总对话时长
- 首个发言人
- 发言调度模式（轮流/按意愿）

## 实现细节
- 每个人的发言对所有人可见（包括发言人和发言内容）
- 所有发言记录存log
- 到总输出max_token或到时间上限，停止对话（在每次发言前检查，不中断发言）
- 停止对话后由总结agent（不是任何一个发言人）读log，总结过程和最终结果
- 外部（人类）可以强行介入发言（在前一个人发言时预约下轮说话，跳过发言调度步骤）
- llm base url和api key放在.env
- 用户自己去写每个人的system prompt 和共享背景prompt有点太费劲，在配置页右侧可以调出多轮对话助手agent，根据用户要求自动填入或修改system prompt 和共享背景prompt（填入/修改需要用户approve）
- 要等最新的一个人说完话（最新一个人的发言进入每个人的上下文）再进入发言调度环节
- 外部（人类）可以随时中断对话进行
- 历史对话和配置持久化

### 发言调度
可选下面的实现或轮流发言，人数为2时仅轮流发言。人数>2时轮流发言顺序为手动/随机（首个发言人确定）。

#### 符号定义

设有 $n$ 个 Agent，对于第 $t$ 轮发言的选择：

| 符号 | 含义 |
| --- | --- |
| $s_i \in [0, 100]$ | Agent $i$ 自报的发言意愿分（LLM 输出） |
| $c_i$ | Agent $i$ 的近期发言热度（惩罚量） |
| $\lambda$ | 抗垄断惩罚系数 |
| $\tau$ | Softmax 温度，控制随机性 |
| $s_i'$ | 惩罚后的有效分数 |
| $P_i$ | 最终被选中发言的概率 |

#### 完整计算流程

**第 1 步：意愿评分**

每个 Agent 看完最新 log 后独立输出意愿分 $s_i$。

**第 2 步：计算近期发言热度 $c_i$**

用指数衰减记忆，让"越近发言"惩罚越重、"越久之前发言"惩罚越轻：

$$
c_i = \sum_{k=1}^{t-1} \gamma^{\,t-1-k} \cdot \mathbb{1}[\text{第 } k \text{ 轮发言者} = i]
$$

其中 $\gamma \in (0, 1)$ 是衰减因子（如 $\gamma = 0.7$），$\mathbb{1}[\cdot]$ 是指示函数。

增量更新更高效：每轮结束后，先对所有人衰减，再给刚发言者加 1：

$$
c_i \leftarrow \gamma \cdot c_i + \mathbb{1}[\text{本轮发言者} = i]
$$

**第 3 步：惩罚后的有效分数**

$$
s_i' = s_i - \lambda \cdot c_i
$$

**第 4 步：Softmax 归一化（含温度）**

$$
P_i = \frac{\exp(s_i' / \tau)}{\displaystyle\sum_{j=1}^{n} \exp(s_j' / \tau)}
$$

**第 5 步：按 $P_i$ 采样**

从分布 $\{P_1, P_2, \dots, P_n\}$ 中随机抽取一个 Agent 作为本轮发言者。

#### 合并成单个公式

$$
P_i = \frac{\exp\!\left(\dfrac{s_i - \lambda c_i}{\tau}\right)}{\displaystyle\sum_{j=1}^{n} \exp\!\left(\dfrac{s_j - \lambda c_j}{\tau}\right)}
$$

#### 数值稳定性（实现必做）

Softmax 直接算 $\exp$ 容易溢出，标准做法是减去最大值（不改变结果）：

$$
m = \max_{j}\left(\frac{s_j'}{\tau}\right), \qquad
P_i = \frac{\exp\!\left(\dfrac{s_i'}{\tau} - m\right)}{\displaystyle\sum_{j} \exp\!\left(\dfrac{s_j'}{\tau} - m\right)}
$$

#### 参考实现

```python
import numpy as np

def select_speaker(scores, heat, last_speaker=None,
                   lam=2.0, tau=1.5, forbid_consecutive=True):
    """
    scores: list[float]  每个 Agent 的意愿分 s_i  (0~10)
    heat:   list[float]  每个 Agent 的近期热度 c_i
    lam:    抗垄断惩罚系数 λ
    tau:    Softmax 温度 τ
    return: 被选中的 Agent 索引
    """
    s = np.array(scores, dtype=float)
    c = np.array(heat, dtype=float)
    # 有效分数
    s_eff = (s - lam * c) / tau

    # 数值稳定的 softmax
    s_eff -= np.max(s_eff[np.isfinite(s_eff)])  # 减最大值
    exp = np.exp(s_eff)
    probs = exp / exp.sum()
    # 按概率采样
    return int(np.random.choice(len(probs), p=probs))


def update_heat(heat, speaker_idx, gamma=0.7):
    """每轮发言后更新热度：先衰减，再给发言者加 1"""
    heat = [gamma * h for h in heat]
    heat[speaker_idx] += 1.0
    return heat
```

#### 参数调参直觉

| 参数 | 调大的效果 |
| --- | --- |
| $\tau$ | 更随机、更平均，弱化意愿分差异 |
| $\tau \to 0$ | 退化为贪心（几乎总选有效分最高者） |
| $\lambda$ | 抗垄断更强，强制轮换更频繁 |
| $\gamma$ | 惩罚记忆更长，一个人要"沉默更久"才恢复 |

建议初始值：$\tau = 1.5$，$\lambda = 2.0$，$\gamma = 0.7$，跑几轮后按观感微调。
