import os

def fix_renames():
    files_to_check = [
        "core/config.py", "core/metrics.py", "routers/user.py",
        "static/home.html", "static/index.html", "static/login.html", "static/register.html",
        "ai_analyzer.py", "app.py", "auth.py", "database.py", "DEPLOY.md",
        "market_data.py", "models.py", "notifier.py", "pre_filter.py",
        "README.md", "trade_logger.py"
    ]

    for filepath in files_to_check:
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as file:
                content = file.read()
            
            new_content = content.replace('TradingView AI Signal Server', 'QuantPilot AI')
            new_content = new_content.replace('TradingView Signal Server', 'QuantPilot AI')
            
            if content != new_content:
                with open(filepath, 'w', encoding='utf-8') as file:
                    file.write(new_content)
                print(f"Correctly updated with UTF-8: {filepath}")

if __name__ == '__main__':
    fix_renames()
