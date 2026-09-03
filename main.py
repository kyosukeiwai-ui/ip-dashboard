import os
import re
import json
import uuid
import base64
import requests
import warnings
import datetime
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from typing import TypedDict, List, Dict, Any, Tuple, Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

# --- OAuth 2.0 / Session Imports ---
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth, OAuthError

warnings.filterwarnings("ignore")
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END
from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

load_dotenv()

app = FastAPI(title="Executive IP Dashboard API")
templates = Jinja2Templates(directory="templates")

# ==============================================================================
# セキュリティ設定: OAuth 2.0 & Session Middleware
# ==============================================================================
# セッション暗号化キー（本番環境では設定必須）
session_secret = os.getenv("SESSION_SECRET")
if not session_secret:
    if os.getenv("K_SERVICE"):  # Cloud Run 本番環境
        raise RuntimeError("CRITICAL: 本番環境で SESSION_SECRET 環境変数が設定されていません。")
    session_secret = "dev-insecure-key-local-only"

app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    https_only=os.getenv("HTTPS_ONLY", "true").lower() == "true",
    same_site="lax",
)

oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# .env / Secret Manager に定義された許可ドメインリスト（カンマ区切り、@の有無や空白を正規化）
raw_allowed = os.getenv("ALLOWED_DOMAINS", "")
ALLOWED_DOMAINS = [d.strip().lstrip("@").lower() for d in raw_allowed.split(",") if d.strip().lstrip("@")]

async def get_current_user(request: Request):
    """Cookieからユーザー情報を取得。未ログインなら/loginへリダイレクト。"""
    user = request.session.get('user')
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )
    return user

# ==============================================================================
# ルーティング: 認証フロー (堅牢化版)
# ==============================================================================
@app.get("/login")
async def login(request: Request):
    """Googleのログイン画面へ転送"""
    redirect_uri = request.url_for('auth').replace(scheme="https")
    return await oauth.google.authorize_redirect(request, str(redirect_uri))

@app.get("/auth")
async def auth(request: Request):
    """Googleでの認証完了後のコールバック処理"""
    try:
        token = await oauth.google.authorize_access_token(request)
        user = token.get('userinfo')
    except OAuthError:
        raise HTTPException(status_code=400, detail="認証に失敗しました")

    if not user or not isinstance(user, dict):
        raise HTTPException(status_code=400, detail="ユーザー情報を取得できませんでした")

    # セキュリティ: ドメインの検証
    user_email = user.get("email", "").strip().lower()
    domain = user_email.split("@")[-1] if "@" in user_email else ""
    
    # ログ出力（個人情報はマスク）
    masked_email = (user_email[:2] + "***@" + domain) if domain else "unknown"
    print(f"[AUTH] ログイン試行: {masked_email}, ドメイン: {domain}")
    
    # ドメインが空、または許可ドメインリストが未設定、またはリストに含まれない場合は拒否
    if not domain or not ALLOWED_DOMAINS or domain not in ALLOWED_DOMAINS:
        print(f"[AUTH] 🚫 アクセス拒否: {domain} は許可リストにありません。")
        raise HTTPException(status_code=403, detail=f"許可されていないドメインです: {domain}")

    print(f"[AUTH] ✅ アクセス許可: {domain} は許可リストに存在します。")

    # 検証成功: セッション保存してトップへ
    request.session['user'] = dict(user)
    return RedirectResponse(url='/')

# ==============================================================================
# ヘルパー関数群 (元のコードそのまま)
# ==============================================================================
def find_all_keys_in_json(obj: Any, target_key: str) -> List[Any]:
    results = []
    if isinstance(obj, dict):
        if target_key in obj:
            val = obj[target_key]
            if isinstance(val, list):
                results.extend(val)
            else:
                results.append(val)
        for v in obj.values():
            results.extend(find_all_keys_in_json(v, target_key))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_all_keys_in_json(item, target_key))
    return results

def extract_text_deeply(node: Any) -> str:
    if isinstance(node, dict):
        if "$" in node:
            return str(node["$"])
        texts: List[str] = []
        for v in node.values():
            extracted = extract_text_deeply(v)
            if extracted:
                texts.append(extracted)
        return " ".join(texts)
    elif isinstance(node, list):
        texts_list: List[str] = []
        for i in node:
            extracted_item = extract_text_deeply(i)
            if extracted_item:
                texts_list.append(extracted_item)
        return " ".join(texts_list)
    return str(node) if node is not None else ""

def fetch_arxiv_documents(query: str, max_results: int) -> Tuple[List[Document], List[dict]]:
    print(f"[API] Arxiv へリクエスト... Query: '{query}'")
    url = f"http://export.arxiv.org/api/query?search_query=all:{query}&start=0&max_results={max_results}"
    try:
        response = requests.get(url, timeout=15)
        root = ET.fromstring(response.content)
        docs: List[Document] = []
        csv_data: List[dict] = []
        
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            title_elem = entry.find("{http://www.w3.org/2005/Atom}title")
            title = str(title_elem.text).replace("\n", " ").strip() if title_elem is not None and title_elem.text else "Unknown Title"
            
            summary_elem = entry.find("{http://www.w3.org/2005/Atom}summary")
            summary = str(summary_elem.text).replace("\n", " ").strip() if summary_elem is not None and summary_elem.text else "No summary available."
            
            id_elem = entry.find("{http://www.w3.org/2005/Atom}id")
            doc_id = str(id_elem.text).split('/')[-1] if id_elem is not None and id_elem.text else f"arxiv-{uuid.uuid4().hex[:6]}"
            
            published_elem = entry.find("{http://www.w3.org/2005/Atom}published")
            pub_year = str(published_elem.text)[:4] if published_elem is not None and published_elem.text else "N/A"
            
            authors_list: List[str] = []
            for a in entry.findall("{http://www.w3.org/2005/Atom}author"):
                name_node = a.find("{http://www.w3.org/2005/Atom}name")
                if name_node is not None and name_node.text is not None:
                    authors_list.append(str(name_node.text))
            author_str = ", ".join(authors_list) if authors_list else "Unknown Author"

            content = f"Title: {title}\nSummary: {summary}"
            metadata = {"id": doc_id, "title": title, "author": author_str, "summary": summary, "published": pub_year}
            docs.append(Document(page_content=content, metadata=metadata))
            csv_data.append(metadata)
        return docs, csv_data
    except Exception as e:
        print(f"Arxiv APIエラー: {e}")
        return [], []

def _extract_epo_data(doc: dict) -> dict:
    pub_num = "Unknown"
    if isinstance(doc, dict):
        if "@country" in doc and "@doc-number" in doc:
            pub_num = str(doc.get("@country", "")) + str(doc.get("@doc-number", "")) + str(doc.get("@kind", ""))
    if pub_num == "Unknown" or not pub_num:
        doc_id_nodes = find_all_keys_in_json(doc, "document-id")
        if doc_id_nodes:
            doc_id_node = doc_id_nodes[0]
            if isinstance(doc_id_node, dict):
                num = doc_id_node.get("doc-number", {}).get("$", "")
                if num: pub_num = str(doc_id_node.get("country", {}).get("$", "")) + str(num) + str(doc_id_node.get("kind", {}).get("$", ""))
    if pub_num == "Unknown" or not pub_num: pub_num = f"PAT-{uuid.uuid4().hex[:4]}"

    title = "Unknown Title"
    title_nodes = find_all_keys_in_json(doc, "invention-title")
    if title_nodes:
        extracted_title = extract_text_deeply(title_nodes[0]).strip()
        if extracted_title: title = extracted_title

    applicant = "Unknown Applicant"
    app_nodes = find_all_keys_in_json(doc, "applicant-name")
    if app_nodes:
        extracted_app = extract_text_deeply(app_nodes[0]).strip()
        if extracted_app: applicant = extracted_app

    summary = "No abstract available"
    abstract_nodes = find_all_keys_in_json(doc, "abstract")
    if abstract_nodes:
        extracted_summary = extract_text_deeply(abstract_nodes[0]).strip()
        if extracted_summary:
            summary = extracted_summary[:300] + "..." 

    return {"pub_num": pub_num, "title": title[:100], "applicant": applicant[:100], "summary": summary, "status": "Published"}

def fetch_epo_documents(cql_query: str, max_results: int) -> Tuple[List[Document], List[dict], int]:
    consumer_key = os.environ.get("EPO_CONSUMER_KEY")
    consumer_secret = os.environ.get("EPO_CONSUMER_SECRET")
    if not consumer_key or not consumer_secret: raise ValueError("EPO APIキーが設定されていません。")
        
    print(f"[API] EPO OPS へリクエスト... CQL: '{cql_query}'")
    auth_url = "https://ops.epo.org/3.2/auth/accesstoken"
    cql_query_encoded = urllib.parse.quote(cql_query, safe='')
    search_url = f"https://ops.epo.org/3.2/rest-services/published-data/search/biblio?q={cql_query_encoded}"
    
    try:
        b64_auth = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
        token_res = requests.post(auth_url, headers={"Authorization": f"Basic {b64_auth}"}, data={"grant_type": "client_credentials"})
        if token_res.status_code != 200: raise ValueError(f"EPO Auth Error: {token_res.text}")
        access_token = token_res.json().get("access_token")
        
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "X-OPS-Range": f"1-{max_results}"}
        search_res = requests.get(search_url, headers=headers, timeout=15)
        
        if search_res.status_code == 404:
            return [], [], 0
        elif search_res.status_code != 200: 
            return [], [], 0
            
        data = search_res.json()
        
        root_data = data.get("ops:world-patent-data", {})
        search_info = root_data.get("ops:biblio-search", {})
        total_count = int(search_info.get("@total-result-count", "0"))

        results = find_all_keys_in_json(data, "exchange-document")
            
        docs: List[Document] = []
        csv_data: List[dict] = []
        for doc in results:
            extracted = _extract_epo_data(doc)
            content = f"PubNum: {extracted['pub_num']}\nTitle: {extracted['title']}\nApplicant: {extracted['applicant']}\nSummary: {extracted['summary']}"
            docs.append(Document(page_content=content, metadata=extracted))
            csv_data.append(extracted)
            
        return docs, csv_data, total_count
    except Exception as e:
        print(f"EPO API通信エラー: {e}")
        return [], [], 0

# ==============================================================================
# LangGraph 定義 (元のコードそのまま)
# ==============================================================================
class AgentState(TypedDict):
    theme: str
    output_language: str
    fetch_max: int
    retrieve_k: int
    raw_academic_data: str
    raw_patent_data: str
    raw_market_data: str
    top_academic_list: list     
    top_patent_list: list       
    full_academic_list: list    
    full_patent_list: list      
    final_dashboard_json: dict

class MiniPatentItem(BaseModel):
    pub_num: str
    title: str
    applicant: str

class LandscapeReason(BaseModel):
    title: str
    description: str
    patent_pub_nums: List[str] = Field(
        description="Extract 2 or 3 exact 'PubNum' (Publication Numbers) from the provided [Patent Data] to show as representative examples. Do NOT invent numbers."
    )

class LandscapeReasons(BaseModel):
    red_ocean: LandscapeReason
    white_space: LandscapeReason
    
class Metrics(BaseModel):
    academic_heat: int = Field(description="学術熱度（0から100のパーセンテージ値で指定してください。例: 85）")
    patent_whitespace: int = Field(description="特許ホワイトスペース度（0から100のパーセンテージ値で指定してください。例: 60）")
    fto_risk: int = Field(description="FTOリスク（0から100のパーセンテージ値で指定してください。例: 45）")

class LandscapeShortDesc(BaseModel):
    red_ocean: str
    white_space: str

class MarketPlayer(BaseModel):
    name: str
    share: str
    revenue: str
    strength: str

class MarketInsights(BaseModel):
    overview: str
    players: List[MarketPlayer]

class FinalDashboardDataLLM(BaseModel):
    overall_judgment: str
    executive_summary: str
    metrics: Metrics
    buzzwords: List[str]
    landscape_short_desc: LandscapeShortDesc
    market_insights: MarketInsights
    action_plans: List[str] = Field(
        description="List of actionable plans. EACH plan MUST start with an appropriate department name enclosed in brackets, e.g., 【経営企画】, 【開発部】, 【事業推進】, 【人事部】."
    )
    landscape_reasons: LandscapeReasons


def academic_agent_node(state: AgentState):
    llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0)
    response = llm.invoke(f"以下の技術テーマをArxiv検索するための英語のキーワード（3単語程度、AND等の演算子不要）に変換してください。テーマ: {state['theme']}。出力はキーワード文字列のみ。")
    
    content = response.content
    if isinstance(content, list):
        query = "".join([str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content])
    else:
        query = str(content)
    
    docs, csv_data = fetch_arxiv_documents(query.strip().replace(" ", "+"), state["fetch_max"])
    if not docs: return {"raw_academic_data": "No academic data found.", "top_academic_list": [], "full_academic_list": []}
        
    embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    vectorstore = Chroma.from_documents(documents=docs, embedding=embeddings, collection_name=f"arxiv_{uuid.uuid4().hex[:8]}")
    retrieved_docs = vectorstore.as_retriever(search_kwargs={"k": state["retrieve_k"]}).invoke(state["theme"])
    try:
        vectorstore.delete_collection()
    except Exception:
        pass
    
    result_text = "\n\n".join([f"[Rank {i+1}] {d.page_content}" for i, d in enumerate(retrieved_docs)])
    top_list = [{"id": d.metadata.get("id", ""), "title": d.metadata.get("title", ""), "author": d.metadata.get("author", ""), "published": d.metadata.get("published", ""), "summary": str(d.metadata.get("summary", ""))[:150] + "..."} for d in retrieved_docs]
    return {"raw_academic_data": result_text, "top_academic_list": top_list, "full_academic_list": csv_data}

def patent_agent_node(state: AgentState):
    llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0)
    
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
    
    テーマ: {state['theme']}"""
    
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
        
    ipc_list = [ipc for ipc in ipc_list if len(ipc) >= 3]
    b_words = [w for w in b_words if w]
    
    def build_cql(ipcs: list[str], core: str, b_ws: list[str]) -> str:
        ipc_cond = ""
        if ipcs:
            ipc_cond = " or ".join([f'ic="{ipc}"' for ipc in ipcs])
            ipc_cond = f"({ipc_cond})"
            
        if len(b_ws) >= 2:
            kw_cond = f'ta all "{core}" and (ta all "{b_ws[0]}" or ta all "{b_ws[1]}")'
        elif len(b_ws) == 1:
            kw_cond = f'ta all "{core}" and ta all "{b_ws[0]}"'
        else:
            kw_cond = f'ta all "{core}"'
            
        if ipc_cond and kw_cond: return f"{ipc_cond} and ({kw_cond})"
        elif ipc_cond: return ipc_cond
        else: return kw_cond

    THRESHOLD = 50 
    
    cql_l1 = build_cql(ipc_list, core_word, b_words)
    best_docs, best_csv, best_count = fetch_epo_documents(cql_l1, state["fetch_max"])
    
    if best_count < THRESHOLD and b_words:
        cql_l2 = build_cql(ipc_list, core_word, [])
        docs, csv_data, count = fetch_epo_documents(cql_l2, state["fetch_max"])
        if count > best_count:
            best_docs, best_csv, best_count = docs, csv_data, count

        if count < THRESHOLD and ipc_list:
            cql_l3 = build_cql([], core_word, b_words)
            docs, csv_data, count = fetch_epo_documents(cql_l3, state["fetch_max"])
            if count > best_count:
                best_docs, best_csv, best_count = docs, csv_data, count
                
            if count < THRESHOLD:
                cql_l4 = build_cql([], core_word, [])
                docs, csv_data, count = fetch_epo_documents(cql_l4, state["fetch_max"])
                if count > best_count:
                    best_docs, best_csv, best_count = docs, csv_data, count

    best_docs = best_docs[:state["fetch_max"]]
    best_csv = best_csv[:state["fetch_max"]]

    if not best_docs: 
        return {"raw_patent_data": "No patent data found.", "top_patent_list": [], "full_patent_list": []}
    
    embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    vectorstore = Chroma.from_documents(documents=best_docs, embedding=embeddings, collection_name=f"patent_{uuid.uuid4().hex[:8]}")
    retrieved_docs = vectorstore.as_retriever(search_kwargs={"k": state["retrieve_k"]}).invoke(state["theme"])
    try:
        vectorstore.delete_collection()
    except Exception:
        pass
    
    result_text = f"[EPO Total Hits: {best_count:,} patents found worldwide in this domain.]\n\n"
    result_text += "\n\n".join([f"[Rank {i+1}] {d.page_content}" for i, d in enumerate(retrieved_docs)])
    
    top_list = [{"pub_num": d.metadata.get("pub_num", ""), "title": d.metadata.get("title", ""), "applicant": d.metadata.get("applicant", ""), "status": d.metadata.get("status", ""), "summary": d.metadata.get("summary", "")} for d in retrieved_docs]
    return {"raw_patent_data": result_text, "top_patent_list": top_list, "full_patent_list": best_csv}

def market_agent_node(state: AgentState):
    llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0.2)
    response = llm.invoke(f"以下のテーマの市場動向について、代表的な企業、市場シェア、売上規模感、強みを箇条書きで教えてください。\nテーマ: {state['theme']}")
    
    content = response.content
    if isinstance(content, list):
        market_text = "".join([str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content])
    else:
        market_text = str(content)
        
    return {"raw_market_data": market_text}

def synthesis_node(state: AgentState):
    lang = state.get("output_language", "ja")
    lang_instruction = "Japanese" if lang == "ja" else "English"
    llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0.2).with_structured_output(FinalDashboardDataLLM)
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", f"""あなたはCEOを補佐する経営戦略のプロフェッショナルAIです。検索結果と市場データを分析しJSONを生成してください。言語: **{lang_instruction}**。
        【重要要件】
        1. [Patent Data]の冒頭にある「EPO Total Hits（総ヒット件数）」の規模感を考慮し、文章の中に「世界で約〇〇件の特許出願が存在し〜」といった具体的な事実を含めてください。
        2. 全体的に簡潔に（従来の半分の文字数で）出力してください。冗長な技術解説は不要です。
        3. 「overall_judgment」と「executive_summary」は、技術や知財戦略に限定せず、会社として今後どのようなビジネスアクションを取るべきか（アライアンス、M&A、特定分野への投資など）の指針を端的に述べる「経営層向けサマリー」として記述してください。
        架空のデータを捏造しないでください。"""),
        ("human", "Theme: {theme}\n\n[Academic Data]\n{academic}\n\n[Patent Data]\n{patent}\n\n[Market Data]\n{market}")
    ])
    
    result = (prompt | llm).invoke({"theme": state["theme"], "academic": state["raw_academic_data"], "patent": state["raw_patent_data"], "market": state["raw_market_data"]})
    
    final_json: dict = {}
    if isinstance(result, BaseModel):
        final_json = result.model_dump()
    elif isinstance(result, dict):
        final_json = result
    else:
        final_json = dict(result) # type: ignore
    
    applicants = [doc.get("applicant") for doc in state.get("full_patent_list", []) if doc.get("applicant") and "Unknown" not in doc.get("applicant", "")]
    counter = Counter(applicants)
    top_applicants = counter.most_common(4)
    key_players = []
    for i, (app_name, count) in enumerate(top_applicants):
        level = "High" if i < 2 else "Medium"
        key_players.append({"name": f"{app_name} (出願:{count}件)", "threat_level": level})
    if not key_players:
        key_players = [{"name": "データ不足により解析不可", "threat_level": "Low"}]
    final_json["key_players"] = key_players

    def resolve_patents(pub_nums: List[str], full_list: List[dict], fallback_list: List[dict]) -> List[dict]:
        resolved = []
        for pid in pub_nums:
            clean_pid = re.sub(r'[^a-zA-Z0-9]', '', pid)
            for p in full_list:
                p_num = re.sub(r'[^a-zA-Z0-9]', '', p.get("pub_num", ""))
                if clean_pid and (clean_pid in p_num or p_num in clean_pid):
                    resolved.append({"pub_num": p["pub_num"], "title": p.get("title", ""), "applicant": p.get("applicant", "")})
                    break
        if not resolved and fallback_list:
            p = fallback_list[0]
            resolved.append({"pub_num": p.get("pub_num", ""), "title": p.get("title", ""), "applicant": p.get("applicant", "")})
            fallback_list.pop(0) 
        return resolved

    ro_nums = final_json.get("landscape_reasons", {}).get("red_ocean", {}).get("patent_pub_nums", [])
    ws_nums = final_json.get("landscape_reasons", {}).get("white_space", {}).get("patent_pub_nums", [])
    
    top_pats = state.get("top_patent_list", [])
    full_pats = state.get("full_patent_list", [])
    
    ro_fallback = top_pats[:len(top_pats)//2] if top_pats else []
    ws_fallback = top_pats[len(top_pats)//2:] if top_pats else []
    if not ro_fallback: ro_fallback = list(top_pats)
    if not ws_fallback: ws_fallback = list(top_pats)

    final_json["landscape_reasons"]["red_ocean"]["patents"] = resolve_patents(ro_nums, full_pats, ro_fallback)
    final_json["landscape_reasons"]["white_space"]["patents"] = resolve_patents(ws_nums, full_pats, ws_fallback)
    
    final_json["landscape_reasons"]["red_ocean"].pop("patent_pub_nums", None)
    final_json["landscape_reasons"]["white_space"].pop("patent_pub_nums", None)

    current_year = datetime.datetime.now().year
    trend_labels = [str(y) for y in range(current_year - 5, current_year + 1)]
    pub_years = [item.get("published") for item in state.get("full_academic_list", []) if item.get("published")]
    counts = Counter(pub_years)
    
    final_json["trend_labels"] = trend_labels
    final_json["trend_data"] = [counts.get(year, 0) for year in trend_labels]
    final_json["academic_list"] = state.get("top_academic_list", [])
    final_json["patent_list"] = state.get("top_patent_list", [])
    
    return {"final_dashboard_json": final_json}

workflow = StateGraph(AgentState)
workflow.add_node("academic_agent", academic_agent_node)
workflow.add_node("patent_agent", patent_agent_node)
workflow.add_node("market_agent", market_agent_node)
workflow.add_node("synthesis", synthesis_node)
workflow.set_entry_point("academic_agent")
workflow.add_edge("academic_agent", "patent_agent")
workflow.add_edge("patent_agent", "market_agent")
workflow.add_edge("market_agent", "synthesis")
workflow.add_edge("synthesis", END)
langgraph_app = workflow.compile()

# ==============================================================================
# FastAPI エンドポイント (保護適用)
# ==============================================================================
UI_DICT = {
    "ja": {
        "PAGE_TITLE": "知財戦略エグゼクティブ・サマリー", "DATE_LABEL": "分析日:", 
        "EXECUTOR_LABEL": "実行者/著作権者:", "EXECUTOR_NAME": "Venture Support Japan LLC.",
        "ALERT_LABEL": "定点観測アラート", 
        "JUDGMENT_LABEL": "経営層向けサマリー:",
        "INDICATOR_LABEL": "主要インジケーター",
        "HEAT_LABEL": "学術熱度", "WHITESPACE_LABEL": "特許ホワイトスペース度", "FTO_LABEL": "FTOリスク",
        "ACADEMIC_TITLE": "Academic Agent (学術)", "ACADEMIC_MAT": "▼ 注目キーワード", "ACADEMIC_TREND": "▼ 論文発表トレンド",
        "BTN_ACAD_LIST": "論文リスト表示", "BTN_ACAD_DL": "リストDL(全件)",
        "PATENT_TITLE": "Patent Agent (特許)", "PATENT_DENS": "▼ ランドスケープ密度",
        "RO_LABEL": "レッドオーシャン", "WS_LABEL": "ホワイトスペース", "BTN_REASON": "根拠を見る",
        "PATENT_PLAYERS": "▼ 注目特許出願人", "BTN_PAT_LIST": "特許リスト表示", "BTN_PAT_DL": "リストDL(全件)",
        "MARKET_TITLE": "Market Agent (市場動向)", "MARKET_OVERVIEW": "▼ 市場概況",
        "TH_MKT_PLAYER": "メインプレイヤー", "TH_MKT_SHARE": "推定シェア", "TH_MKT_REV": "売上規模", "TH_MKT_STR": "強み",
        "ACTION_TITLE": "Action Plans", "BTN_ACTION": "一括で指示", "BTN_SAVE_HTML": "ダッシュボードを保存(HTML)",
        "MODAL_CLOSE": "閉じる",
        "TH_ACAD_MODAL": "抽出論文リスト (Top)", "TH_PAT_MODAL": "関連特許リスト (Top)",
        "TH_ID": "ID", "TH_TITLE": "タイトル", "TH_SUMMARY": "概要", "TH_CIT": "発行年", "TH_PUB": "特許番号", "TH_INV": "発明の名称", "TH_APP": "出願人", "TH_REP": "代表特許",
        "JS_ALERT": "指示を送信しました。", "CSV_ACAD_HEAD": "ID,タイトル,著者,要約,発行年\\n", "CSV_PAT_HEAD": "特許番号,名称,出願人,要約,状況\\n",
        "DISCLAIMER_TEXT": "<strong>免責事項:</strong> 本ダッシュボードはAI（Gemini 3.1 Flash Lite）による初期仮説の提供を目的としています。最終的なFTO評価・出願判断は法務・知財部門の専門家と実施してください。"
    },
    "en": {
        "PAGE_TITLE": "IP Strategy Executive Summary", "DATE_LABEL": "Analysis Date:", 
        "EXECUTOR_LABEL": "Executor/Copyright:", "EXECUTOR_NAME": "Venture Support Japan LLC.",
        "ALERT_LABEL": "Monitoring Alert", 
        "JUDGMENT_LABEL": "Executive Summary:",
        "INDICATOR_LABEL": "Key Indicators",
        "HEAT_LABEL": "Academic Heat", "WHITESPACE_LABEL": "Patent Whitespace", "FTO_LABEL": "FTO Risk",
        "ACADEMIC_TITLE": "Academic Agent", "ACADEMIC_MAT": "▼ Key Keywords", "ACADEMIC_TREND": "▼ Publication Trends",
        "BTN_ACAD_LIST": "Show Paper List", "BTN_ACAD_DL": "Download Papers (All)",
        "PATENT_TITLE": "Patent Agent", "PATENT_DENS": "▼ Landscape Density",
        "RO_LABEL": "Red Ocean", "WS_LABEL": "White Space", "BTN_REASON": "View Evidence",
        "PATENT_PLAYERS": "▼ Key Patent Applicants", "BTN_PAT_LIST": "Show Patent List", "BTN_PAT_DL": "Download Patents (All)",
        "MARKET_TITLE": "Market Agent", "MARKET_OVERVIEW": "▼ Market Overview",
        "TH_MKT_PLAYER": "Key Player", "TH_MKT_SHARE": "Est. Share", "TH_MKT_REV": "Revenue", "TH_MKT_STR": "Strengths",
        "ACTION_TITLE": "Action Plans", "BTN_ACTION": "Execute All", "BTN_SAVE_HTML": "Save Dashboard (HTML)",
        "MODAL_CLOSE": "Close",
        "TH_ACAD_MODAL": "Extracted Papers (Top)", "TH_PAT_MODAL": "Related Patents (Top)",
        "TH_ID": "ID", "TH_TITLE": "Title", "TH_SUMMARY": "Summary", "TH_CIT": "Year", "TH_PUB": "Patent No.", "TH_INV": "Invention Title", "TH_APP": "Applicant", "TH_REP": "Representative",
        "JS_ALERT": "Instruction sent.", "CSV_ACAD_HEAD": "ID,Title,Author,Summary,Year\\n", "CSV_PAT_HEAD": "PatentNo,Title,Applicant,Summary,Status\\n",
        "DISCLAIMER_TEXT": "<strong>Disclaimer:</strong> This dashboard is generated by AI (Gemini 3.1 Flash Lite) to provide initial hypotheses. Please consult legal/IP professionals for final FTO evaluations and filing decisions."
    }
}

# 【重要】エンドポイントの引数に Depends(get_current_user) を追加し、保護を有効化しました
@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})

@app.post("/analyze", response_class=HTMLResponse)
def analyze_theme(
    request: Request,
    theme: str = Form(...),
    lang_code: str = Form("ja"),
    fetch_max: int = Form(30, ge=1, le=100),
    retrieve_k: int = Form(5, ge=1, le=30),
    user: dict = Depends(get_current_user)
):
    if not os.getenv("GOOGLE_API_KEY"):
        return HTMLResponse("<h1>エラー: GOOGLE_API_KEY が設定されていません。</h1>", status_code=500)

    try:
        final_state = langgraph_app.invoke({
            "theme": theme, 
            "output_language": lang_code,
            "fetch_max": fetch_max,
            "retrieve_k": retrieve_k,
            "raw_academic_data": "", "raw_patent_data": "", "raw_market_data": "",
            "top_academic_list": [], "top_patent_list": [], "full_academic_list": [], "full_patent_list": [],
            "final_dashboard_json": {}
        })
        
        json_data = json.dumps(final_state["final_dashboard_json"], ensure_ascii=False)
        csv_data = json.dumps({
            "academic_list": final_state.get("full_academic_list", []),
            "patent_list": final_state.get("full_patent_list", [])
        }, ensure_ascii=False)
        
        ui_text = UI_DICT.get(lang_code, UI_DICT["ja"])

        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "theme": theme,
            "json_data": json_data,
            "csv_data": csv_data,
            "ui": ui_text,
            "user": user
        })
        
    except Exception as e:
        print(f"[ERROR] 分析実行エラー: {e}")
        return HTMLResponse(
            "<h1>分析中にエラーが発生しました</h1><p>時間をおいて再度お試しいただくか、管理者へお問い合わせください。</p>",
            status_code=500
        )