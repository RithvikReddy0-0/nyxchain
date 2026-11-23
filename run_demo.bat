@echo off
REM NyxChain: install dependencies, run the tests, both demos and the reputation simulation
cd /d "%~dp0"
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python scripts\demo.py
python scripts\attack_demo.py
python scripts\reputation_sim.py --out reputation_sim_out
pause
