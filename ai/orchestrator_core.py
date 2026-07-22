import os
import math
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "odin_ai", "db", "odin_archive.db")

class NeuralRoutingMatrix:
    def __init__(self):
        self.agent_profiles = {
            "Archivist": ["ingest", "file", "hash", "duplicate", "register", "scan", "staging", "db"],
            "Analyzer":  ["read", "book", "text", "summary", "profile", "keywords", "content", "analyze"],
            "Vision":    ["image", "picture", "pixel", "png", "jpg", "render", "view", "ocr", "frame"]
        }
        
    def _tokenize_clean(self, text):
        return set(text.lower().replace("?", "").replace(".", "").split())

    def _cosine_similarity(self, input_tokens, profile_tokens):
        intersection = input_tokens.intersection(profile_tokens)
        if not intersection or not input_tokens:
            return 0.0
        return len(intersection) / (math.sqrt(len(input_tokens)) * math.sqrt(len(profile_tokens)))

    def route_intent(self, user_query):
        query_tokens = self._tokenize_clean(user_query)
        scores = {}
        for agent_name, profile_keywords in self.agent_profiles.items():
            score = self._cosine_similarity(query_tokens, set(profile_keywords))
            scores[agent_name] = score
        best_agent = max(scores, key=scores.get)
        if scores[best_agent] == 0.0:
            return "Orchestrator (Direct Query Execution)", scores
        return best_agent, scores

class AgentOrchestrator:
    def __init__(self):
        self.router = NeuralRoutingMatrix()
        
    def execute_command(self, user_query):
        print(f"\n[Command Received]: '{user_query}'")
        target_agent, matrix_scores = self.router.route_intent(user_query)
        
        print("[-] Current Neural Routing Matrix Coefficients:")
        for agent, score in matrix_scores.items():
            print(f"    ↳ Vector alignment for {agent:10}: {score:.4f}")
            
        print(f"[!] Target Acquired ──► Dispatching to: **{target_agent} Agent**")
        self._dispatch(target_agent, user_query)

    def _dispatch(self, agent_name, query):
        if agent_name == "Archivist":
            print("[Action] Booting ingest_manager.py to check isolated file buffers...")
            
        elif agent_name == "Analyzer":
            print("[Action] Summoning analyzer_agent.py to extract semantic knowledge...")
            try:
                from analyzer_agent import TextAnalyzerAgent
                analyzer = TextAnalyzerAgent()
                analyzer.process_all_pending()
            except ImportError:
                print("[!] Error: analyzer_agent.py file not found.")
            
        elif agent_name == "Vision":
            print("[Action] Summoning vision_agent.py to extract structural image profiles...")
            try:
                from vision_agent import VisionMatrixAgent
                vision = VisionMatrixAgent()
                vision.process_all_pending()
            except ImportError:
                print("[!] Error: vision_agent.py file not found.")
            
        else:
            print("[Action] Handling natively via Orchestrator Core baseline memory weights.")

if __name__ == "__main__":
    orchestrator = AgentOrchestrator()
    orchestrator.execute_command("Scan the staging directory for new files and hash them")
    orchestrator.execute_command("Show me the book profile summaries inside the database")
    orchestrator.execute_command("Did we extract any png or jpg image files today?")
