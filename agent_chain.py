#!/usr/bin/env python3
"""
Agent 协作链 - 自动化多步骤任务
v1.1 - 增强触发词
"""
import subprocess
import json
import re
import time
import logging

logger = logging.getLogger(__name__)

class AgentChain:
    """Agent 协作链管理器"""
    
    CHAINS = {
        "code_review": {
            "name": "代码开发+审查",
            "agents": ["developer", "reviewer"],
            "description": "筑微写代码，审微审查"
        },
        "research_publish": {
            "name": "研究+发布",
            "agents": ["researcher", "operator"],
            "description": "探微分析，通微整理推送格式"
        },
        "full_dev": {
            "name": "完整开发流程",
            "agents": ["developer", "reviewer", "operator"],
            "description": "开发→审查→整理发布"
        }
    }
    
    # 扩展触发词
    TRIGGERS = {
        "code_review": [
            "写完帮我审查", "开发并审核", "写好检查一下", 
            "代码写完审一下", "开发并review", "写完review",
            "写完审查", "写好审查", "帮我审一下",
            "代码审查", "写完检查", "开发+审查",
            "写个.*审查", "开发.*review"
        ],
        "research_publish": [
            "分析后整理推送", "研究并发布", "分析完推送",
            "分析后推送", "研究后发布", "整理成推送",
            "分析.*推送", "研究.*发布"
        ],
        "full_dev": [
            "完整开发流程", "从开发到发布", "全流程开发",
            "开发到上线", "完整流程"
        ]
    }
    
    def __init__(self):
        self.timeout = 120
        # 预编译正则
        self._patterns = {}
        for chain_name, triggers in self.TRIGGERS.items():
            patterns = []
            for t in triggers:
                if ".*" in t:
                    patterns.append(re.compile(t))
                else:
                    patterns.append(t)
            self._patterns[chain_name] = patterns
    
    def detect_chain(self, message: str) -> str | None:
        """检测消息是否触发协作链"""
        message_lower = message.lower()
        for chain_name, patterns in self._patterns.items():
            for p in patterns:
                if isinstance(p, re.Pattern):
                    if p.search(message_lower):
                        return chain_name
                else:
                    if p in message_lower:
                        return chain_name
        return None
    
    def _call_agent(self, agent_id: str, message: str, session_id: str) -> dict:
        """调用单个 Agent"""
        cmd = [
            "docker", "exec", "clawdbot",
            "openclaw", "agent", "--local",
            "--agent", agent_id,
            "--message", message,
            "--session-id", session_id,
            "--timeout", str(self.timeout)
        ]
        
        try:
            logger.info(f"调用 Agent: {agent_id}")
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=self.timeout + 10
            )
            
            output = result.stdout.strip()
            if result.returncode != 0:
                error = result.stderr.strip() or "未知错误"
                return {"success": False, "agent": agent_id, "error": error}
            
            return {"success": True, "agent": agent_id, "output": output}
            
        except subprocess.TimeoutExpired:
            return {"success": False, "agent": agent_id, "error": "执行超时"}
        except Exception as e:
            return {"success": False, "agent": agent_id, "error": str(e)}
    
    def _build_handoff(self, prev_result: dict, next_agent: str, original_request: str) -> str:
        """构建交接提示词"""
        prev_output = prev_result.get("output", "")
        
        templates = {
            "reviewer": f"""请审查以下代码，找出问题和改进建议：

【原始需求】
{original_request}

【代码内容】
{prev_output}

审查要点：
1. 正确性和逻辑问题
2. 安全漏洞
3. 性能问题
4. 代码风格
5. 改进建议""",

            "operator": f"""请将以下内容整理为适合推送的格式：

{prev_output}

要求：结构清晰，重点突出，适合阅读""",
        }
        
        return templates.get(next_agent, prev_output)
    
    def execute(self, chain_name: str, message: str, session_id: str) -> dict:
        """执行协作链"""
        chain = self.CHAINS.get(chain_name)
        if not chain:
            return {"success": False, "error": f"未知协作链: {chain_name}"}
        
        agents = chain["agents"]
        results = []
        original_request = message
        
        logger.info(f"开始协作链: {chain['name']} ({' → '.join(agents)})")
        
        for i, agent_id in enumerate(agents):
            if i == 0:
                current_message = message
            else:
                current_message = self._build_handoff(
                    results[-1], 
                    agent_id, 
                    original_request
                )
            
            result = self._call_agent(
                agent_id, 
                current_message, 
                f"{session_id}-chain-{i}"
            )
            results.append(result)
            
            if not result.get("success"):
                logger.error(f"协作链在 {agent_id} 处中断")
                break
            
            if i < len(agents) - 1:
                time.sleep(1)
        
        return {
            "success": all(r.get("success") for r in results),
            "chain": chain_name,
            "chain_name": chain["name"],
            "results": results
        }
    
    def format_result(self, chain_result: dict) -> str:
        """格式化协作链结果"""
        if not chain_result.get("success"):
            failed = [r for r in chain_result.get("results", []) if not r.get("success")]
            if failed:
                return f"❌ 协作链失败\n\n{failed[0]['agent']}: {failed[0].get('error', '未知错误')}"
            return "❌ 协作链执行失败"
        
        results = chain_result.get("results", [])
        chain_name = chain_result.get("chain_name", "协作链")
        
        agent_names = {
            "developer": "⚒️ 筑微",
            "reviewer": "🔍 审微",
            "researcher": "🔬 探微",
            "operator": "📡 通微"
        }
        
        parts = [f"✅ **{chain_name}** 完成\n"]
        
        for r in results:
            agent = r.get("agent", "")
            name = agent_names.get(agent, agent)
            output = r.get("output", "")
            
            parts.append(f"\n{'─'*30}")
            parts.append(f"**{name}**")
            parts.append(f"{'─'*30}")
            parts.append(output)
        
        return "\n".join(parts)


agent_chain = AgentChain()

def detect_chain_intent(message: str) -> str | None:
    return agent_chain.detect_chain(message)

def execute_chain(chain_name: str, message: str, session_id: str) -> str:
    result = agent_chain.execute(chain_name, message, session_id)
    return agent_chain.format_result(result)


if __name__ == "__main__":
    tests = [
        "帮我写一个Python爬虫，写完帮我审查",
        "写个排序算法并审查",
        "分析AI趋势后推送",
        "写个Hello World"
    ]
    for t in tests:
        chain = detect_chain_intent(t)
        print(f"'{t}' -> {chain}")
