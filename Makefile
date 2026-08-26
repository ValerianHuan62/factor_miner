.PHONY: install test test-fast cli dashboard

install:
	uv sync --frozen --extra dashboard

test:
	uv run python -m unittest discover -s tests

test-fast:
	uv run python -m unittest tests.test_synthetic_e2e -v

cli:
	uv run factor-miner --help

dashboard:
	uv run streamlit run dashboard/app.py --server.headless true --server.address 127.0.0.1 --server.port 8501
