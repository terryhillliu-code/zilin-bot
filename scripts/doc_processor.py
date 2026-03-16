#!/usr/bin/env python3
"""
文档处理脚本
处理 ~/Documents/Library/【待整理】 目录下的 PDF/EPUB 文件
"""

import os
import re
import shutil
from pathlib import Path
from datetime import datetime

# 分类规则
CATEGORY_RULES = {
    # AI 系统相关
    "10-19_AI-Systems": {
        "keywords": ["AI Agent", "Agent", "Agentic", "RAG", "MCP", "LLM", "大模型", "人工智能", "AI安全", "AI Security", "智能体", "多模态", "Generative AI", "GenAI"],
        "subdirs": {
            "11_大模型架构": ["大模型", "LLM", "GPT", "Transformer"],
            "12_多模态智能体": ["Agent", "Agentic", "智能体", "MCP", "多模态"],
            "14_RAG与知识系统": ["RAG", "知识", "检索"],
            "15_AI应用研究": ["AI应用", "AI营销", "AI模拟"],
        }
    },
    # AI 硬件相关
    "20-29_AI-Hardware": {
        "keywords": ["GPU", "芯片", "算力", "HBM", "存储", "内存", "液冷", "硬件", "量子", "Quantum"],
        "subdirs": {
            "21_AI芯片架构": ["芯片", "GPU", "NPU"],
            "22_GPU与加速器": ["GPU", "加速器"],
            "23_存储与内存": ["存储", "内存", "HBM", "SSD"],
            "24_互连与通信": ["互连", "通信"],
        }
    },
    # 基础设施
    "30-39_Infra-Compute": {
        "keywords": ["数据中心", "液冷", "服务器", "HPC", "高性能计算", "云计算", "云原生"],
        "subdirs": {
            "32_数据中心": ["数据中心", "液冷"],
            "33_高性能计算": ["HPC", "高性能计算"],
        }
    },
    # 网络
    "40-49_Networking": {
        "keywords": ["网络", "Networking", "互连", "光通信"],
        "subdirs": {}
    },
    # 行业研究
    "50-59_Industry": {
        "keywords": ["行业报告", "行业研究", "市场分析", "经济发展", "数字", "经济", "报告", "白皮书", "趋势"],
        "subdirs": {
            "51_行业报告": ["报告", "白皮书", "研究", "展望", "分析"],
            "52_技术趋势": ["趋势", "展望", "发展"],
        }
    },
}

def classify_file(filename: str) -> tuple[str, str]:
    """
    根据文件名分类
    返回: (主目录, 子目录)
    """
    filename_lower = filename.lower()

    # 检查每个分类的关键词
    for main_dir, rules in CATEGORY_RULES.items():
        for keyword in rules["keywords"]:
            if keyword.lower() in filename_lower:
                # 找到匹配的主目录，再找子目录
                for subdir, sub_keywords in rules.get("subdirs", {}).items():
                    for sub_kw in sub_keywords:
                        if sub_kw.lower() in filename_lower:
                            return main_dir, subdir
                return main_dir, ""

    # 默认分类
    return "50-59_Industry", "51_行业报告"


def clean_filename(filename: str) -> str:
    """清理文件名，移除来源标记"""
    # 移除 Z-Library 标记
    patterns = [
        r'\s*\(Z-Library\)',
        r'\s*\(z-library\.sk, 1lib\.sk, z-lib\.sk\)',
        r'\s*\(for [^.]+\)',
        r'\s*\(First Early Release\)',
    ]
    result = filename
    for pattern in patterns:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
    return result.strip()


def generate_note_filename(title: str) -> str:
    """生成笔记文件名"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    # 清理标题
    safe_title = re.sub(r'[<>:"/\\|?*]', '', title)
    safe_title = safe_title[:60]  # 限制长度
    return f"{date_str}_{safe_title}.md"


def main():
    source_dir = Path("~/Documents/Library/【待整理】").expanduser()
    library_dir = Path("~/Documents/Library").expanduser()
    inbox_dir = Path("~/Documents/ZhiweiVault/Inbox").expanduser()

    # 扫描文件
    files = list(source_dir.glob("*.pdf")) + list(source_dir.glob("*.epub"))

    print(f"找到 {len(files)} 个文件待处理\n")

    results = []
    for f in files:
        original_name = f.name
        clean_name = clean_filename(original_name)
        main_dir, subdir = classify_file(original_name)

        target_dir = library_dir / main_dir
        if subdir:
            target_dir = target_dir / subdir

        results.append({
            "original": original_name,
            "cleaned": clean_name,
            "main_dir": main_dir,
            "subdir": subdir,
            "target_dir": str(target_dir),
        })

        print(f"📄 {original_name[:50]}...")
        print(f"   → {main_dir}/{subdir or '(根目录)'}")
        print(f"   清理后: {clean_name[:50]}...")
        print()

    return results


if __name__ == "__main__":
    main()