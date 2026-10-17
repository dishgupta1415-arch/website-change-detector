# 🌐 Website Change Detector

A web-based application that monitors websites for content changes, tracks website availability, and sends notifications when changes are detected.

## 📌 About the Project

The **Website Change Detector** is designed to help users automatically monitor websites without manually checking them again and again. Users can add website URLs, check their availability, view change history, and receive notifications when website content changes.

The project uses a FastAPI backend and a web-based dashboard to manage website monitoring.

## ✨ Features

- **Website Monitoring:** Add and monitor website URLs.
- **Content Change Detection:** Detect changes in webpage titles and text content.
- **Scheduled Checks:** Configure monitoring intervals.
- **Uptime Monitoring:** Track whether a website is available or unavailable.
- **Change History:** View previously detected website changes.
- **Email Notifications:** Receive alerts about detected changes.
- **Telegram Notifications:** Get monitoring alerts through Telegram.
- **Dashboard:** Manage monitored websites and view alerts.
- **REST API:** Access monitoring functionality through FastAPI endpoints.

## 🛠️ Technologies Used

- **Backend:** Python, FastAPI
- **Frontend:** HTML, CSS, JavaScript
- **Database:** SQLite
- **Browser Automation:** Playwright
- **Task Scheduling:** APScheduler
- **Notifications:** Email SMTP and Telegram Bot API

## 📂 Project Structure

```text
Website-Change-Detector/
├── main.py
├── index.html
├── monitoring_extras.py
├── websites.db
├── .env
├── requirements.txt
└── README.md
```

*Note: Update this structure to match the files in your actual project. Do not upload your private `.env` file or database if it contains sensitive information.*

## ⚙️ Installation and Setup

### 1. Clone the Repository

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd Website-Change-Detector
```

Replace `YOUR_GITHUB_REPOSITORY_URL` with your repository's actual URL.

### 2. Create a Virtual Environment

```bash
python -m venv venv
```

Activate it on Windows:

```bash
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

Install the Playwright browser if required:

```bash
playwright install chromium
```

### 4. Configure Environment Variables

Create a local `.env` file and configure the email and Telegram credentials required by your application.

**Important:** Never commit API tokens, email passwords, or other secrets to GitHub.

### 5. Run the Application

```bash
uvicorn main:app --reload
```

Open the dashboard at:

http://127.0.0.1:8000

Open the interactive API documentation at:

http://127.0.0.1:8000/docs

## 🚀 How to Use

1. Start the FastAPI server.
2. Open the dashboard in your browser.
3. Add the website URL you want to monitor.
4. Configure the monitoring interval if supported.
5. Check website availability and content changes.
6. View alerts and change history in the dashboard.

## 🔔 Notifications

The application can send email and Telegram notifications when configured. Notification delivery depends on valid credentials and the corresponding services being available.

## 🔒 Security

- Keep credentials and API tokens in environment variables.
- Validate URLs before accessing external websites.
- Block localhost, private IP addresses, and unsafe redirects.
- Monitor only websites you are authorized to access.

## 🔮 Future Enhancements

- Visual webpage comparison using screenshots
- AI-generated summaries of website changes
- Interactive uptime analytics and graphs
- SSL certificate expiry notifications
- PDF and CSV report export
- Custom CSS selector-based monitoring
- User authentication and personalized dashboards

*These are planned enhancements and should not be presented as completed features until implemented.*

## 👨‍💻 Author

**Dish Gupta**

Information Technology Student | VIT Vellore

## 📄 License

This project is available for educational and learning purposes. Add a license file if you intend to specify permissions for reuse and distribution.
