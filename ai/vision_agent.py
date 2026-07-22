import os
import struct
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "odin_ai", "db", "odin_archive.db")
STAGING_DIR = os.path.join(BASE_DIR, "odin_ai", "staging")

class VisionMatrixAgent:
    """Specialist Agent that reads raw image binary headers completely offline."""
    def __init__(self):
        pass

    def parse_image_dimensions(self, filename):
        file_path = os.path.join(STAGING_DIR, filename)
        if not os.path.exists(file_path):
            return None

        try:
            with open(file_path, "rb") as f:
                head = f.read(24)
                if len(head) < 24:
                    return "Corrupted Image Header"

                # Check for PNG Signature
                if head.startswith(b'\x89PNG\r\n\x1a\n'):
                    # PNG dimensions are stored as 4-byte big-endian ints starting at byte 16
                    w, h = struct.unpack('>ii', head[16:24])
                    return f"PNG Format | Dimensions: {w}x{h} px"

                # Check for JPEG Signature
                elif head.startswith(b'\xff\xd8'):
                    f.seek(0)
                    size_data = f.read()
                    # Scan for the Start of Frame (SOF0) marker
                    idx = size_data.find(b'\xff\xc0')
                    if idx != -1:
                        # Extract structural dimensions (height, width)
                        h, w = struct.unpack('>HH', size_data[idx+5:idx+9])
                        return f"JPEG Format | Dimensions: {w}x{h} px"
                
                return "Unknown Image Binary Structure"
        except Exception as e:
            return f"Parsing Error: {str(e)}"

    def process_all_pending(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Select images logged by the archivist
        cursor.execute("""
            SELECT id, filename FROM file_archive 
            WHERE (file_type = 'png' OR file_type = 'jpg' OR file_type = 'jpeg')
            AND processing_status = 'Unprocessed'
        """)
        records = cursor.fetchall()
        
        if not records:
            print("[*] Vision Agent found 0 pending image assets.")
            conn.close()
            return

        for record_id, filename in records:
            print(f"[-] Vision Agent analyzing structural geometry of: {filename}")
            image_profile = self.parse_image_dimensions(filename)
            
            if image_profile:
                cursor.execute("""
                    UPDATE file_archive 
                    SET profile_summary = ?, processing_status = 'Analyzed' 
                    WHERE id = ?
                """, (image_profile, record_id))
                print(f"[+] Structural profile committed for asset ID {record_id}.")

        conn.commit()
        conn.close()

if __name__ == "__main__":
    vision = VisionMatrixAgent()
    vision.process_all_pending()
