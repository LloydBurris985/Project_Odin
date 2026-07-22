import json
import requests
import sqlite3
import sys
import ast
import os
import time
import uuid
import importlib.util

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5-coder:0.5b"
DB_PATH = "odin_archive.db"

KERNEL_VERSION = "1.5.0"
PLANNER_VERSION = "2.0.0"

# SESSION 1 FROZEN SCHEMA: CORE BUILT-IN CAPABILITIES
CAPABILITY_GRAPH = {
    "db_search": {
        "version": "1.0.0",
        "engine": "Knowledge Engine",
        "inputs": ["search_term"],
        "outputs": ["records", "match_count"],
        "dependencies": [],
        "est_runtime": 0.1,
        "cpu_cost": "Low",
        "mem_cost": "Low",
        "side_effects": "Read-Only",
        "permission_level": "User",
        "confidence": 0.95,
        "description": "Need source location or context matching metadata"
    },
    "ast_map": {
        "version": "1.0.0",
        "engine": "Script Engine",
        "inputs": ["file_name"],
        "outputs": ["classes", "functions", "line_count"],
        "dependencies": ["db_search"],
        "est_runtime": 0.3,
        "cpu_cost": "Medium",
        "mem_cost": "Low",
        "side_effects": "Read-Only",
        "permission_level": "User",
        "confidence": 0.98,
        "description": "Need structure layout and definition extraction"
    },
    "generate_docs": {
        "version": "1.0.0",
        "engine": "Documentation Engine",
        "inputs": ["classes", "functions"],
        "outputs": ["doc_string"],
        "dependencies": ["ast_map"],
        "est_runtime": 0.4,
        "cpu_cost": "Low",
        "mem_cost": "Low",
        "side_effects": "Write-File",
        "permission_level": "User",
        "confidence": 0.90,
        "description": "Generate clean docstrings and architecture reports"
    }
}

# SESSION 6: POLICY & GOVERNANCE ENGINE
class PolicyEngine:
    ALLOWED_PERMISSIONS = ["User", "Admin"]

    @classmethod
    def validate_capability(cls, cap_key, meta):
        perm = meta.get("permission_level", "User")
        if perm not in cls.ALLOWED_PERMISSIONS:
            return False, f"Permission violation: Capability '{cap_key}' requires '{perm}' access."
        return True, "OK"

# SESSION 6: ADAPTIVE PLANNER
class AdaptivePlanner:
    @staticmethod
    def query_historical_avg(cap_key, fallback_est):
        try:
            if not os.path.exists(DB_PATH):
                return fallback_est
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT AVG(duration_sec) FROM telemetry_logs 
                WHERE capability_key = ? AND status = 'SUCCESS'
            """, (cap_key,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0] is not None:
                return round(row[0], 4)
        except Exception:
            pass
        return fallback_est

# SESSION 5: TELEMETRY RECORDER
class TelemetryRecorder:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    capability_key TEXT,
                    engine TEXT,
                    status TEXT,
                    duration_sec REAL,
                    cpu_cost TEXT,
                    mem_cost TEXT,
                    error_msg TEXT,
                    timestamp REAL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  [Telemetry SDK] DB Init Error: {e}")

    def log_execution(self, session_id, cap_key, meta, status, duration, error_msg=""):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO telemetry_logs 
                (session_id, capability_key, engine, status, duration_sec, cpu_cost, mem_cost, error_msg, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id,
                cap_key,
                meta.get("engine", "Unknown"),
                status,
                duration,
                meta.get("cpu_cost", "Low"),
                meta.get("mem_cost", "Low"),
                error_msg,
                time.time()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  [Telemetry SDK] Write Error: {e}")

# SESSION 4: WORKFLOW TEMPLATE LIBRARY
class WorkflowLibrary:
    def __init__(self, workflow_file="workflows.json"):
        self.workflow_file = workflow_file
        self.workflows = {}
        self.load_workflows()

    def load_workflows(self):
        if os.path.exists(self.workflow_file):
            try:
                with open(self.workflow_file, "r", encoding="utf-8") as f:
                    self.workflows = json.load(f)
                print(f"  [Workflow Library] Loaded {len(self.workflows)} cached workflow templates")
            except Exception as e:
                print(f"  [Workflow Library] Failed to load workflows: {e}")

    def match_workflow(self, query):
        query_lower = query.lower()
        for wf_id, wf_data in self.workflows.items():
            for kw in wf_data.get("trigger_keywords", []):
                if kw in query_lower:
                    return wf_id, wf_data
        return None, None

# SESSION 3: DYNAMIC PLUGIN REGISTRY
class PluginRegistry:
    def __init__(self, plugin_dir="plugins"):
        self.plugin_dir = plugin_dir
        self.handlers = {}

    def load_plugins(self):
        if not os.path.exists(self.plugin_dir):
            os.makedirs(self.plugin_dir)
            return

        for fname in os.listdir(self.plugin_dir):
            if fname.endswith(".py") and not fname.startswith("__"):
                fpath = os.path.join(self.plugin_dir, fname)
                mod_name = fname[:-3]
                try:
                    spec = importlib.util.spec_from_file_location(mod_name, fpath)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)

                    for attr_name in dir(mod):
                        attr = getattr(mod, attr_name)
                        if isinstance(attr, type) and hasattr(attr, "get_capabilities"):
                            instance = attr()
                            caps = instance.get_capabilities()
                            meta = getattr(instance, "META", {"name": attr_name})
                            
                            for cap_name, cap_schema in caps.items():
                                CAPABILITY_GRAPH[cap_name] = cap_schema
                                self.handlers[cap_name] = instance
                                print(f"  [Plugin SDK] Registered '{cap_name}' from {meta.get('name')} [v{cap_schema.get('version')}]")
                except Exception as e:
                    print(f"  [Plugin SDK] Failed to load plugin {fname}: {e}")

# BUILT-IN TOOL IMPLEMENTATIONS
def run_db_search(target):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cursor.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            return {"status": "SUCCESS", "records": [], "match_count": 0, "warnings": ["No active tables in workspace db"]}
        cursor.execute(f"PRAGMA table_info({tables[0]});")
        cols = [c[1] for c in cursor.fetchall()]
        search_col = "summary" if "summary" in cols else cols[0]
        cursor.execute(f"SELECT * FROM {tables[0]} WHERE {search_col} LIKE ?", (f"%{target}%",))
        res = cursor.fetchall()
        conn.close()
        return {"status": "SUCCESS", "records": res, "match_count": len(res), "warnings": []}
    except Exception as e:
        return {"status": "DB_ERROR", "error_msg": str(e)}

def run_ast_map(target):
    if not target.endswith(".py") and "." not in target:
        target += ".py"
    if not os.path.exists(target):
        return {"status": "FILE_NOT_FOUND", "target": target}
    try:
        with open(target, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        cls = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        fnc = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        return {"status": "SUCCESS", "classes": cls, "functions": fnc, "line_count": len(src.splitlines()), "warnings": []}
    except Exception as e:
        return {"status": "PARSE_ERROR", "error_msg": str(e)}

# SESSION 2: DETERMINISTIC PARALLEL SCHEDULER
class ParallelScheduler:
    @staticmethod
    def build_schedule_tiers(selected_caps):
        extended_caps = set(selected_caps)
        added = True
        while added:
            added = False
            for cap in list(extended_caps):
                if cap in CAPABILITY_GRAPH:
                    for dep in CAPABILITY_GRAPH[cap]["dependencies"]:
                        if dep not in extended_caps:
                            extended_caps.add(dep)
                            added = True

        executed = set()
        tiers = []
        remaining = list(extended_caps)

        while remaining:
            current_tier = []
            for cap in remaining[:]:
                if cap in CAPABILITY_GRAPH:
                    if all(dep in executed for dep in CAPABILITY_GRAPH[cap]["dependencies"]):
                        current_tier.append(cap)
                        remaining.remove(cap)
                else:
                    remaining.remove(cap)
            if not current_tier:
                if remaining:
                    tiers.append(remaining)
                break
            
            for cap in current_tier:
                executed.add(cap)
            tiers.append(current_tier)
            
        return tiers

# ENGINE-AGNOSTIC CORE KERNEL CONSTRUCTS
class ExecutionEngine:
    def __init__(self, target, plugin_registry, telemetry_recorder):
        self.session_id = str(uuid.uuid4())
        self.workspace = {
            "context": {
                "session_id": self.session_id,
                "timestamp": time.time(),
                "kernel_version": KERNEL_VERSION,
                "planner_version": PLANNER_VERSION
            },
            "target": target
        }
        self.registry = plugin_registry
        self.telemetry = telemetry_recorder

    def execute_tiers(self, tiers):
        print("\n[Execution Engine] Resolving graph sequence across parallel tiers...")
        pipeline_start = time.time()
        logged_tasks = 0
        
        for tier_idx, tier in enumerate(tiers, 1):
            is_parallel = len(tier) > 1
            print(f"\n⚡ Tier {tier_idx} Execution Block " + ("(🚀 PARALLEL BUCKET)" if is_parallel else "(🔒 SEQUENTIAL BUCKET)"))
            
            for cap in tier:
                meta = CAPABILITY_GRAPH[cap]
                
                # SESSION 6: POLICY SECURITY CHECK
                allowed, reason = PolicyEngine.validate_capability(cap, meta)
                if not allowed:
                    print(f"     ✗ Governance Policy Blocked '{cap}': {reason}")
                    print("   [Circuit Breaker]: Halting execution due to policy violation.")
                    return False

                print(f"  ↳ Running: {meta['engine']} -> {cap}('{self.workspace['target']}') [v{meta['version']}]")
                start_time = time.time()
                
                if cap in self.registry.handlers:
                    plugin_instance = self.registry.handlers[cap]
                    out = plugin_instance.execute_capability(cap, self.workspace["target"], self.workspace)
                elif cap == "db_search":
                    out = run_db_search(self.workspace["target"])
                elif cap == "ast_map":
                    out = run_ast_map(self.workspace["target"])
                else:
                    out = {"status": "SUCCESS", "warnings": []}
                    
                elapsed = time.time() - start_time
                status = out.get("status", "UNKNOWN_ERROR")
                err_msg = out.get("error_msg", "")
                
                self.telemetry.log_execution(
                    session_id=self.session_id,
                    cap_key=cap,
                    meta=meta,
                    status=status,
                    duration=elapsed,
                    error_msg=err_msg
                )
                logged_tasks += 1
                
                if status == "SUCCESS":
                    print(f"     ✓ Success! Profile: [CPU: {meta['cpu_cost']} | Mem: {meta['mem_cost']}] ({elapsed:.4f}s)")
                    if "line_count" in out:
                        print(f"       Parsed {out['line_count']} lines. Classes: {out['classes']}")
                    if "file_path" in out:
                        print(f"       Plugin Output File: {out['file_path']}")
                    if "sector_data" in out:
                        print(f"       Sector Grid Data: {out['sector_data']}")
                    self.workspace[f"{cap}_output"] = out
                else:
                    print(f"     ✗ Fatal Contract Error Triggered: [{status}]")
                    print("   [Circuit Breaker]: Halting downstream tiers immediately to ensure safety.")
                    return False
                    
        total_pipeline_time = time.time() - pipeline_start
        print(f"\n[Telemetry] Logged {logged_tasks} task metric(s) to '{DB_PATH}' | Total Duration: {total_pipeline_time:.4f}s")
        return True

def display_explainable_plan(goal_text, tiers, source="Planner"):
    flat_caps = [c for tier in tiers for c in tier if c in CAPABILITY_GRAPH]
    
    # SESSION 6: ADAPTIVE HISTORICAL RUNTIME CALCULATION
    total_time = sum(
        AdaptivePlanner.query_historical_avg(c, CAPABILITY_GRAPH[c]["est_runtime"]) 
        for c in flat_caps
    )
    
    unique_engines = len(set(CAPABILITY_GRAPH[c]["engine"] for c in flat_caps))
    has_writes = any(CAPABILITY_GRAPH[c]["side_effects"] == "Write-File" for c in flat_caps)
    risk_profile = "Write-File (Modifies Workspace)" if has_writes else "Read-only (Safe)"
    parallel_capable = sum(1 for t in tiers if len(t) > 1)
    
    print("\n┌─── ODIN EXPLAINABLE PLAN ───")
    print(f"│ Source:    {source}")
    print(f"│ Goal:      Execute {goal_text}")
    print("│ Reasoning (Scheduled Structure):")
    for idx, tier in enumerate(tiers, 1):
        mode = "Parallel" if len(tier) > 1 else "Sequential"
        print(f"│   Tier {idx} [{mode}]:")
        for cap in tier:
            if cap in CAPABILITY_GRAPH:
                meta = CAPABILITY_GRAPH[cap]
                hist_avg = AdaptivePlanner.query_historical_avg(cap, meta["est_runtime"])
                print(f"│     • {meta['description']} → {meta['engine']} [Avg: {hist_avg:.4f}s]")
    print("│")
    print(f"│ Adaptive Est. Time: {total_time:.4f} s")
    print(f"│ Modules:            {unique_engines}")
    print(f"│ Parallel Tiers:     {parallel_capable}")
    print(f"│ Risk Profile:       {risk_profile}")
    print("└─────────────────────────────")

if __name__ == "__main__":
    print(f"─── Project Odin Kernel Active [v{KERNEL_VERSION} LTS] ───")
    
    registry = PluginRegistry()
    registry.load_plugins()
    
    workflow_lib = WorkflowLibrary()
    telemetry_recorder = TelemetryRecorder()
    
    SYSTEM_PROMPT = f"""You are the Task Planner for Project Odin.
Identify the core subject/asset the user wants to inspect and the capabilities required.
Available capabilities: {list(CAPABILITY_GRAPH.keys())}.
Respond ONLY with a valid JSON object matching this schema:
{{
  "target": "asset_or_file_name",
  "capabilities": ["cap1", "cap2"]
}}"""

    while True:
        try:
            msg = input("\nOdin > ")
            if msg.lower() in ['exit', 'quit']: break
            if not msg.strip(): continue
            
            wf_id, wf_match = workflow_lib.match_workflow(msg)
            
            if wf_match:
                plan_source = f"Workflow Library Cache Hits -> Template '{wf_data_name if (wf_data_name := wf_match.get('name')) else wf_id}'"
                target_asset = wf_match.get("target_fallback", "odin_chat")
                selected_caps = wf_match.get("capabilities", [])
                
                words = msg.split()
                for word in words:
                    if "." in word or word.endswith(".py"):
                        target_asset = word
            else:
                plan_source = "Dynamic LLM Planner / Graph Resolution"
                target_asset = "chart_generator"
                selected_caps = ["db_search", "ast_map"]
                
                try:
                    r = requests.post(OLLAMA_URL, json={"model": MODEL_NAME, "prompt": f"{SYSTEM_PROMPT}\n\nUser: {msg}", "stream": False, "options": {"temperature": 0.0}})
                    txt = r.json().get("response", "").strip()
                    if "```json" in txt: txt = txt.split("```json")[1].split("```")[0].strip()
                    parsed_res = json.loads(txt)
                    
                    if isinstance(parsed_res, dict):
                        target_asset = parsed_res.get("target", "chart_generator")
                        selected_caps = parsed_res.get("capabilities", ["db_search", "ast_map"])
                except:
                    if "save" in msg.lower() or "lattice" in msg.lower():
                        target_asset = "sprite_ship_alpha"
                        selected_caps = ["save_asset", "open_coordinate"]
            
            if "." in target_asset and not target_asset.endswith(".py"):
                target_asset = target_asset.split(".")[0]
                
            tiers = ParallelScheduler.build_schedule_tiers(selected_caps)
            display_explainable_plan(target_asset, tiers, source=plan_source)
            
            engine = ExecutionEngine(target=target_asset, plugin_registry=registry, telemetry_recorder=telemetry_recorder)
            engine.execute_tiers(tiers)
            
        except KeyboardInterrupt:
            sys.exit(0)
