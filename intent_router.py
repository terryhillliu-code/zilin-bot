"""意图路由器 - 根据用户消息判断由哪个Agent处理

⭐ v64.0: 已废弃 researcher/operator Agent，此路由器仅用于兼容性
实际路由由 zhiwei_agent.router.IntentClassifier 处理
"""
import re


class IntentRouter:
    # ⭐ v64.0: 精简路由规则，仅保留 developer（用于代码开发）
    ROUTE_RULES = {
        "developer": {
            "keywords": ["代码", "脚本", "bug", "错误", "编程", "开发", "部署", "配置", "修复", "python", "shell", "docker", "api", "接口", "数据库", "架构", "实现", "函数", "测试"],
            "patterns": [r"帮我写.*(代码|脚本|程序|函数)", r"怎么(实现|修复|部署|配置|安装)", r"(报错|异常|崩溃|不工作)", r"(python|shell|docker|git|npm)"]
        },
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
