"""意图路由器 - 根据用户消息判断由哪个Agent处理"""
import re


class IntentRouter:
    ROUTE_RULES = {
        "researcher": {
            "keywords": ["分析", "研究", "调研", "趋势", "报告", "对比", "行业", "市场", "竞品", "深度", "洞察", "评估", "新闻", "资讯", "动态", "情报", "追踪", "论文", "数据", "统计", "预测"],
            "patterns": [r"帮我(分析|研究|调研|评估)", r"(什么|哪些)(趋势|动态|变化)", r"(对比|比较).*(和|与|跟)", r"(行业|市场|领域).*(分析|报告|趋势)"]
        },
        "developer": {
            "keywords": ["代码", "脚本", "bug", "错误", "编程", "开发", "部署", "配置", "修复", "python", "shell", "docker", "api", "接口", "数据库", "架构", "实现", "函数", "测试"],
            "patterns": [r"帮我写.*(代码|脚本|程序|函数)", r"怎么(实现|修复|部署|配置|安装)", r"(报错|异常|崩溃|不工作)", r"(python|shell|docker|git|npm)"]
        },
        "reviewer": {
            "keywords": ["审查", "review", "检查代码", "代码审计", "找bug", "漏洞", "安全检查", "代码质量"],
            "patterns": [r"帮我(审查|检查|review)", r"(代码|脚本).*(问题|bug|漏洞)", r"(检查|审计).*(代码|安全)"]
        },
        "operator": {
            "keywords": ["推送", "格式", "排版", "订阅", "取消", "频率", "通知", "消息", "模板", "卡片", "美化", "定时", "静默"],
            "patterns": [r"推送(太多|太少|格式|频率)", r"(消息|通知)(格式|模板|样式)", r"(订阅|取消订阅|退订)"]
        }
    }

    @classmethod
    def route(cls, message: str) -> str:
        if not message or len(message) < 2:
            return "main"
        message_lower = message.lower().strip()
        if message_lower.startswith("/") or message_lower.startswith("m"):
            return "main"
        scores = {"main": 1}
        for agent, rules in cls.ROUTE_RULES.items():
            score = 0
            for kw in rules["keywords"]:
                if kw in message_lower:
                    score += 1
            for pattern in rules["patterns"]:
                if re.search(pattern, message_lower):
                    score += 3
            scores[agent] = score
        best = max(scores, key=scores.get)
        if best != "main" and scores[best] >= 3:
            print(f"🔀 路由: {best} (分数: {scores})")
            return best
        return "main"

    @classmethod
    def explain(cls, message: str) -> str:
        message_lower = message.lower().strip()
        scores = {"main": 1}
        details = []
        for agent, rules in cls.ROUTE_RULES.items():
            score = 0
            matched_kw = []
            matched_pt = []
            for kw in rules["keywords"]:
                if kw in message_lower:
                    score += 1
                    matched_kw.append(kw)
            for pattern in rules["patterns"]:
                if re.search(pattern, message_lower):
                    score += 3
                    matched_pt.append(pattern)
            scores[agent] = score
            if score > 0:
                details.append(f"  {agent}: {score}分 (关键词: {matched_kw})")
        best = max(scores, key=scores.get)
        result = best if best != "main" and scores[best] >= 3 else "main"
        return f"路由结果: {result}\n分数: {scores}\n" + "\n".join(details)
