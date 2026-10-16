# Website Change Detector

A web-based monitoring application built with **Python, FastAPI, and SQLite** to monitor websites, detect content changes, and track website uptime.

## Features

- **Website Monitoring:** Monitor websites for content changes.
- **Change Detection:** Detect when website content changes.
- **Uptime Monitoring:** Check whether websites are UP or DOWN.
- **Email Notifications:** Receive email alerts for detected changes, downtime, and recovery.
- **Dashboard:** Manage and monitor websites through a web interface.
- **SQLite Database:** Store website monitoring data locally.

## Tech Stack

- Python
- FastAPI
- SQLite
- HTML, CSS, JavaScript
- Uvicorn

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/dishgupta1415-arch/website-change-detector.git
   ```

2. Open the project folder:

   ```bash
   cd website-change-detector
   ```

3. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   ```

   Windows PowerShell:

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

4. Install the required dependencies:

   ```bash
   pip install -r requirements.txt
   ```

5. Configure your environment variables in a local `.env` file. Never upload passwords or email credentials to GitHub.

6. Start the application:

   ```bash
   uvicorn main:app --reload
   ```

7. Open your browser at:

   `http://127.0.0.1:8000`

## Security

Keep `.env`, `.venv`, database files, and credentials out of the public repository.

## Author

**GitHub:** [dishgupta1415-arch](https://github.com/dishgupta1415-arch)
