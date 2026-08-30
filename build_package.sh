#!/bin/bash
# Build script for yang-comparator-lite package
# Cross-platform compatible (Linux, Windows via Git Bash, macOS)

set -e  # Exit on error

echo "🔨 Building yang-comparator-lite package..."
echo ""

# Clean previous builds
echo "🧹 Cleaning previous builds..."
rm -rf build dist *.egg-info
rm -rf yang_comparator_lite.egg-info
rm -rf yang_rag.egg-info

# Create a clean temporary structure
echo "📦 Preparing package structure..."

# Ensure only comparator module is included
if [ -d "yang_rag/comparator" ]; then
    echo "✓ Found comparator module"
else
    echo "❌ Error: yang_rag/comparator directory not found!"
    exit 1
fi

# Check for compatibility rules
if [ -f "yang_rag/comparator/compatibility_rules.xml" ]; then
    echo "✓ Found compatibility_rules.xml"
else
    echo "⚠️  Warning: compatibility_rules.xml not found!"
fi

# Install build dependencies
echo ""
echo "📥 Installing build dependencies..."
pip install --upgrade pip setuptools wheel build twine

# Build the package using setup_lite_package.py
echo ""
echo "🏗️  Building distributions..."
python -m build --config-setting="--global-option=--python-tag=py3" -s -w -C--global-option=setup_lite_package.py

# Check if build was successful
if [ -d "dist" ] && [ "$(ls -A dist)" ]; then
    echo ""
    echo "✅ Build successful!"
    echo ""
    echo "📦 Built packages:"
    ls -lh dist/
    echo ""
    echo "📋 Installation instructions:"
    echo ""
    echo "  Local installation (for testing):"
    echo "    pip install dist/yang_comparator_lite-1.0.0-py3-none-any.whl"
    echo ""
    echo "  Or install from source distribution:"
    echo "    pip install dist/yang-comparator-lite-1.0.0.tar.gz"
    echo ""
    echo "  Upload to PyPI (requires account):"
    echo "    twine upload dist/*"
    echo ""
    echo "  Upload to Test PyPI (for testing):"
    echo "    twine upload --repository testpypi dist/*"
    echo ""
    echo "🎉 Package ready for distribution!"
else
    echo ""
    echo "❌ Build failed! Check errors above."
    exit 1
fi
