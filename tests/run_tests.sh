#!/bin/bash
# Run the models_v2 test suite with coverage.
#
# Usage on server:
#   cd /opt/stairs-web-app/tests
#   chmod +x run_tests.sh
#   ./run_tests.sh
#
# This script:
#   1. Installs pytest + pytest-cov if needed
#   2. Sets STAIRS_APP_DIR so tests can import models_v2
#   3. Runs the test suite with verbose output
#   4. Reports coverage percentage and missing lines

set -e

APP_DIR="${STAIRS_APP_DIR:-/opt/stairs-web-app}"
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$TEST_DIR"

# Install pytest + coverage if not present
if ! python3 -c "import pytest" 2>/dev/null; then
    echo "📦 Installing pytest and pytest-cov..."
    pip install pytest pytest-cov --quiet --break-system-packages 2>/dev/null || \
    pip install --user pytest pytest-cov --quiet
fi

export STAIRS_APP_DIR="$APP_DIR"

echo "═══════════════════════════════════════════════════════════════"
echo "  STAIRS models_v2 TEST SUITE"
echo "═══════════════════════════════════════════════════════════════"
echo "  App dir : $APP_DIR"
echo "  Test dir: $TEST_DIR"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Run with coverage. Note: --cov=models_v2 (module name), NOT file path.
python3 -m pytest test_models_v2.py \
    -v \
    --tb=short \
    --cov=models_v2 \
    --cov-report=term-missing \
    2>&1 | tee test_results.log

EXIT_CODE=${PIPESTATUS[0]}

echo ""
echo "═══════════════════════════════════════════════════════════════"
if [ $EXIT_CODE -eq 0 ]; then
    echo "  ✅ ALL TESTS PASSED"
else
    echo "  ❌ TEST FAILURES — see output above"
fi
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Detailed log: $TEST_DIR/test_results.log"

exit $EXIT_CODE
