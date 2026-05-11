import time
import subprocess
import logging
import sys
from datetime import datetime

# Konfigurasi logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Interval scrape dalam detik (1 jam = 3600 detik)
SCRAPE_INTERVAL = 3600

def run_scraper():
    logging.info("Memulai proses pengecekan artikel terbaru di halaman 1...")
    
    # Command untuk menjalankan scraper dengan fokus di page 1 saja
    cmd = [
        sys.executable, "scrape-wabeta.py",
        "--pages", "1",
        "--mongo",
        "--mongo-host", "localhost",
        "--mongo-port", "27018",
        "--mongo-db", "wabetainfo",
        "--mongo-col", "articles",
        "--mongo-user", "admin",
        "--mongo-pass", "wabetainfo"
    ]
    
    try:
        # Menjalankan script scraper dan menangkap outputnya
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logging.info("Pengecekan selesai.")
            
            # Menampilkan log ringkasan dari output scraper
            for line in result.stdout.split('\n'):
                # Hanya menampilkan baris yang relevan agar tidak spam di log
                if "[+] Discovered" in line or "[DB] MongoDB:" in line or "[✓] MongoDB:" in line:
                    logging.info(f"  {line.strip()}")
        else:
            logging.error(f"Scraper gagal dengan exit code {result.returncode}")
            logging.error(f"Error detail:\n{result.stderr}")
            
    except Exception as e:
        logging.error(f"Terjadi kesalahan saat menjalankan scraper: {e}")

def main():
    logging.info("="*50)
    logging.info("WABetaInfo Real-Time Monitor Aktif")
    logging.info(f"Interval: 1 jam ({SCRAPE_INTERVAL} detik)")
    logging.info("="*50)
    
    # Lakukan scrape pertama kali saat script dijalankan
    run_scraper()
    
    # Loop abadi setiap 1 jam
    while True:
        logging.info(f"Menunggu {SCRAPE_INTERVAL} detik (1 jam) untuk pengecekan selanjutnya...\n")
        time.sleep(SCRAPE_INTERVAL)
        run_scraper()

if __name__ == "__main__":
    main()
