.PHONY: clean test

clean:
	find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null || true
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

test:
	pytest -q
