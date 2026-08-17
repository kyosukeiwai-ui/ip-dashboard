import os
import re
import base64
import requests
import urllib.parse
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI

import warnings
warnings.filterwarnings("ignore")

# 環境変数の読み込み
load_dotenv()

def get_epo_token(consumer_key: str, consumer_secret: str) -> str:
    """EPO APIへのアクセストークンを取得する"""
    auth_url = "https://ops.epo.org/3.2/auth/accesstoken"
    b64_auth = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    token_res = requests.post(
        auth_url, 
        headers={"Authorization": f"Basic {b64_auth}"}, 
        data={"grant_type": "client_credentials"}
    )
    if token_res.status_code != 200:
        raise ValueError(f"EPO Auth Error: {token_res.text}")
    return token_res.json().get("access_token")

def search_epo_patents_count(cql_query: str, access_token: str) -> int:
    """
    EPOにCQLクエリを投げて「ヒットした総件数(total-result-count)」を返す関数
    """
    cql_query_encoded = urllib.parse.quote(cql_query, safe='')
    search_url = f"https://ops.epo.org/3.2/rest-services/published-data/search/biblio?q={cql_query_encoded}"
    
    headers = {
        "Authorization": f"Bearer {access_token}", 
        "Accept": "application/json", 
        "X-OPS-Range": "1-1" 
    }
    
    try:
        search_res = requests.get(search_url, headers=headers, timeout=15)
        if search_res.status_code == 404:
            return 0
        elif search_res.status_code != 200: 
            return 0
            
        data = search_res.json()
        root = data.get("ops:world-patent-data", {})
        search_info = root.get("ops:biblio-search", {})
        total_count_str = search_info.get("@total-result-count", "0")
        
        return int(total_count_str)
    except Exception:
        return 0

def extract_search_parameters(theme: str, llm) -> tuple[list[str], str, list[str]]:
    """テーマからIPCの候補(複数)と、コアキーワード・関連キーワードを統合して抽出する"""
    prompt = f"""以下の技術テーマについて、特許調査のプロフェッショナルとして検索パラメータを抽出してください。
    【要件】
    1. 該当しそうな国際特許分類（IPC）のメイングループ（4桁、例: G06F, H04L, A61K）を関連度が高い順に「最大3つ」
    2. 最も重要なコア概念を表す英単語を「1つ」（A）
    3. その概念を絞り込むための別の関連概念に関する英単語を「最大2つ」（B1, B2）
    ※キーワードは必ず「1単語のみ」にしてください（スペースや記号を含めない）。

    【出力フォーマット】
    IPC: <IPC1>, <IPC2>, <IPC3>
    A: <core_word>
    B: <word1>, <word2>
    
    テーマ: {theme}"""
    
    response = llm.invoke(prompt)
    content = response.content
    query_text = "".join([str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content]) if isinstance(content, list) else str(content)
    
    ipc_list = []
    core_word = "technology"
    b_words = []
    
    for line in query_text.split('\n'):
        line = line.strip()
        if line.startswith('IPC:'):
            ipc_text = line.replace('IPC:', '').strip()
            ipc_list = [re.sub(r'[^a-zA-Z0-9]', '', w).upper() for w in ipc_text.split(',')]
        elif line.startswith('A:'):
            core_word = line.replace('A:', '').strip().strip('"\'')
            core_word = re.sub(r'[^a-zA-Z0-9]', '', core_word).strip()
        elif line.startswith('B:'):
            b_text = line.replace('B:', '').strip()
            b_words = [re.sub(r'[^a-zA-Z0-9]', '', w).strip() for w in b_text.split(',')]
            
    if not core_word:
        core_word = "technology"
        
    ipc_list = [ipc for ipc in ipc_list if len(ipc) >= 3] # 空や短すぎるものを除外
    b_words = [w for w in b_words if w]
    
    return ipc_list, core_word, b_words

def build_cql(ipc_list: list[str], core_word: str, b_words: list[str]) -> str:
    """IPCリストとキーワードリストからプロ仕様のCQLを組み立てる"""
    # IPCクエリ部 (OR結合)
    ipc_cond = ""
    if ipc_list:
        ipc_cond = " or ".join([f'ic="{ipc}"' for ipc in ipc_list])
        ipc_cond = f"({ipc_cond})"
        
    # キーワードクエリ部 (AND/OR結合)
    if len(b_words) >= 2:
        kw_cond = f'ta all "{core_word}" and (ta all "{b_words[0]}" or ta all "{b_words[1]}")'
    elif len(b_words) == 1:
        kw_cond = f'ta all "{core_word}" and ta all "{b_words[0]}"'
    else:
        kw_cond = f'ta all "{core_word}"'
        
    # 結合
    if ipc_cond and kw_cond:
        return f"{ipc_cond} and ({kw_cond})"
    elif ipc_cond:
        return ipc_cond
    else:
        return kw_cond

def main():
    print("\n" + "="*60)
    print(" 🚀 EPO Intelligent Search Algorithm Tester")
    print("="*60 + "\n")
    
    epo_key = os.environ.get("EPO_CONSUMER_KEY")
    epo_secret = os.environ.get("EPO_CONSUMER_SECRET")
    
    if not epo_key or not epo_secret:
        print("❌ [エラー] .envファイルに EPO_CONSUMER_KEY と EPO_CONSUMER_SECRET を設定してください。")
        return
        
    try:
        print("⏳ EPOへのアクセストークンを取得中...")
        access_token = get_epo_token(epo_key, epo_secret)
        llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0)
        
        print("-" * 60)
        theme = input("🔎 検索したい技術分野を入力してください:\n> ")
        if not theme.strip():
            return
            
        print("\n" + "=" * 60)
        print(" 【ステップ1】 LLMによる検索パラメータの推論")
        print("=" * 60)
        
        ipc_list, core_word, b_words = extract_search_parameters(theme, llm)
        print(f"🧠 [推定IPC (OR)] : {ipc_list}")
        print(f"🧠 [コアKW (AND)] : {core_word}")
        print(f"🧠 [関連KW (OR)]  : {b_words}")
        
        print("\n" + "=" * 60)
        print(" 【ステップ2】 段階的検索（フォールバック）の実行")
        print("=" * 60)
        
        # しきい値（この件数を下回ったら条件を緩める）
        THRESHOLD = 50 
        
        # --- Level 1: 厳格（IPC × コアKW × 関連KW） ---
        cql_l1 = build_cql(ipc_list, core_word, b_words)
        print(f"\n[Level 1: 最も厳格な絞り込み]")
        print(f"  式: {cql_l1}")
        count_l1 = search_epo_patents_count(cql_l1, access_token)
        print(f"  => ヒット数: 【 {count_l1:,} 件 】")
        
        best_cql, best_count, level_used = cql_l1, count_l1, "Level 1"
        
        # --- Level 2: 緩和（IPC × コアKWのみ） ---
        if count_l1 < THRESHOLD and b_words:
            cql_l2 = build_cql(ipc_list, core_word, [])
            print(f"\n[Level 2: 関連キーワードを外して緩和]")
            print(f"  式: {cql_l2}")
            count_l2 = search_epo_patents_count(cql_l2, access_token)
            print(f"  => ヒット数: 【 {count_l2:,} 件 】")
            if count_l2 > best_count:
                best_cql, best_count, level_used = cql_l2, count_l2, "Level 2"

            # --- Level 3: 分野拡張（IPC指定なし × コアKW × 関連KW） ---
            if count_l2 < THRESHOLD and ipc_list:
                cql_l3 = build_cql([], core_word, b_words)
                print(f"\n[Level 3: IPCの縛りを外して分野を拡張]")
                print(f"  式: {cql_l3}")
                count_l3 = search_epo_patents_count(cql_l3, access_token)
                print(f"  => ヒット数: 【 {count_l3:,} 件 】")
                if count_l3 > best_count:
                    best_cql, best_count, level_used = cql_l3, count_l3, "Level 3"
                    
                # --- Level 4: 最大拡張（IPC指定なし × コアKWのみ） ---
                if count_l3 < THRESHOLD:
                    cql_l4 = build_cql([], core_word, [])
                    print(f"\n[Level 4: 最大拡張 (コアキーワードのみ)]")
                    print(f"  式: {cql_l4}")
                    count_l4 = search_epo_patents_count(cql_l4, access_token)
                    print(f"  => ヒット数: 【 {count_l4:,} 件 】")
                    if count_l4 > best_count:
                        best_cql, best_count, level_used = cql_l4, count_l4, "Level 4"

        print("\n" + "=" * 60)
        print(" 🎯 【最終結果】 採用された最適な検索式")
        print("=" * 60)
        print(f"採用レベル: {level_used}")
        print(f"検索式    : {best_cql}")
        print(f"母集団件数: 【 {best_count:,} 件 】")
        print("=" * 60 + "\n")
        
        # 最終的に採用されたCQL (best_cql) が、main.pyのダッシュボード生成用のクエリになります。
        
    except Exception as e:
        print(f"\n❌ [致命的なエラー]: {e}")

if __name__ == "__main__":
    main()