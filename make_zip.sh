#!/bin/bash
# Script to create a zip archive of the project, excluding Git files and other unwanted files

ZIP_NAME="project_6.zip"
ZIP_IGNORE_FILE=".zipignore"

# Create .zipignore if it doesn't exist
if [ ! -f "$ZIP_IGNORE_FILE" ]; then
    cat > "$ZIP_IGNORE_FILE" <<EOL
.git/*
.gitignore
.gitattributes
$ZIP_NAME
make_zip.sh
EOL
    echo "Created default $ZIP_IGNORE_FILE"
fi

# Run the zip command
echo "Creating $ZIP_NAME..."
zip -r "$ZIP_NAME" . -x@"$ZIP_IGNORE_FILE"

echo "Done. Archive saved as $ZIP_NAME"
