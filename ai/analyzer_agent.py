import os
import json
import sqlite3
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "odin_ai", "db", "odin_archive.db")
STAGING_DIR = os.path.join(BASE_DIR, "odin_ai", "staging")

# Local Ollama endpoint config
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5-coder:0.5b"  # Change this to "llama3" or whatever model you downloaded

class TextAnalyzerAgent:
    """Specialist Agent that uses local LLM weights via Ollama for deep reasoning."""
    
    def _query_local_llm(self, prompt):
        """Sends a request to the local Ollama API using standard libraries."""
        payload = {
            "model": MODEL_NAME,
            "prompt": prompt,
            "stream": False
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL, 
                data=data, 
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                res_body = json.loads(response.read().decode("utf-8"))
                return res_body.get("response", "").strip()
        except Exception as e:
            return f"[LLM Error: Could not reach Ollama service. Details: {e}]"

    def extract_knowledge_profile(self, target_filename):
        file_path = os.path.join(STAGING_DIR, target_filename)
        if not os.path.exists(file_path):
            return None

        print(f"[-] Analyzer summoning local LLM ({MODEL_NAME}) for: {target_filename}")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text_chunk = f.read(2000)  # Read the first 2000 chars to analyze safely

        # Craft an explicit prompt to enforce structured intelligence profiles
        prompt = (
            f"Analyze this raw data snippet captured from a target file named '{target_filename}'. "
            "Provide a one-sentence high-level summary and list 3 core entities/keywords discovered inside. "
            "Be extremely brief and direct.\n\n"
            f"--- DATA START ---\n{text_chunk}\n--- DATA END ---"
        )
        
        return self._query_local_llm(prompt)

    def process_all_pending(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, filename FROM file_archive 
            WHERE file_type = 'txt' AND processing_status = 'Indexed'
        """)
        records = cursor.fetchall()
        
        if not records:
            conn.close()
            return

        for record_id, filename in records:
            ai_profile = self.extract_knowledge_profile(filename)
            if ai_profile:
                cursor.execute("""
                    UPDATE file_archive 
                    SET profile_summary = ?, processing_status = 'Analyzed' 
                    WHERE id = ?
                """, (ai_profile, record_id))
                print(f"[+] Local LLM profile committed for asset ID {record_id}!")

        conn.commit()
        conn.close()

if __name__ == "__main__":
    analyzer = TextAnalyzerAgent()
    analyzer.process_all_pending()
