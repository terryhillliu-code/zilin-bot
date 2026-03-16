#!/usr/bin/env python3
"""
文档批量处理脚本
处理 ~/Documents/Library/【待整理】 根目录下的 PDF/EPUB 文件
"""

import os
import re
import shutil
import json
from pathlib import Path
from datetime import datetime

# 分类规则
CATEGORY_RULES = {
    "10-19_AI-Systems": {
        "keywords": ["AI Agent", "Agent", "Agentic", "RAG", "MCP", "LLM", "大模型", "人工智能", "AI安全", "AI Security", "智能体", "多模态", "Generative AI", "GenAI", "AI模拟", "AI营销", "AI谣言"],
        "priority": 1,
    },
    "20-29_AI-Hardware": {
        "keywords": ["GPU", "芯片", "算力", "HBM", "存储", "内存", "液冷", "硬件", "量子", "Quantum", "服务器", "智算"],
        "priority": 2,
    },
    "30-39_Infra-Compute": {
        "keywords": ["数据中心", "液冷", "HPC", "高性能计算", "云计算", "云原生", "基础设施"],
        "priority": 3,
    },
    "40-49_Networking": {
        "keywords": ["网络", "Networking", "互连", "光通信"],
        "priority": 4,
    },
    "50-59_Industry": {
        "keywords": ["行业", "报告", "白皮书", "趋势", "市场", "经济", "发展", "研究", "洞察", "政府工作"],
        "priority": 5,
    },
    "60-69_Business": {
        "keywords": ["商业", "管理", "创业", "投资", "ROI"],
        "priority": 6,
    },
}

def classify_file(filename: str) -> str:
    """根据文件名分类，返回主目录"""
    filename_lower = filename.lower()

    # 按优先级检查
    for main_dir, rules in sorted(CATEGORY_RULES.items(), key=lambda x: x[1]["priority"]):
        for keyword in rules["keywords"]:
            if keyword.lower() in filename_lower:
                return main_dir

    # 默认分类到行业报告
    return "50-59_Industry"


def clean_filename(filename: str) -> str:
    """清理文件名，移除来源标记"""
    patterns = [
        r'\s*\(Z-Library\)',
        r'\s*\(z-library\.sk, 1lib\.sk, z-lib\.sk\)',
        r'\s*\(for [^)]+\)',
        r'\s*\(First Early Release\)',
    ]
    result = filename
    for pattern in patterns:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
    return result.strip()


def main():
    source_dir = Path("~/Documents/Library/【待整理】").expanduser()
    library_dir = Path("~/Documents/Library").expanduser()

    # 只处理根目录的文件
    files = list(source_dir.glob("*.pdf")) + list(source_dir.glob("*.epub"))

    print(f"找到 {len(files)} 个文件待处理\n")

    # 分类结果
    classification = {}
    for f in sorted(files):
        main_dir = classify_file(f.name)
        clean_name = clean_filename(f.name)

        if main_dir not in classification:
            classification[main_dir] = []

        classification[main_dir].append({
            "original_path": str(f),
            "original_name": f.name,
            "cleaned_name": clean_name,
        })

    # 输出分类结果
    for main_dir in sorted(classification.keys()):
        items = classification[main_dir]
        print(f"\n{'='*60}")
        print(f"📁 {main_dir} ({len(items)} 个文件)")
        print('='*60)
        for item in items:
            print(f"  • {item['cleaned_name'][:60]}")

    # 输出 JSON 供后续处理
    output_file = Path("~/Documents/Library/【待整理】/_classification.json").expanduser()
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(classification, f, ensure_ascii=False, indent=2)
    print(f"\n\n分类结果已保存到: {output_file}")

    return classification


if __name__ == "__main__":
    main()