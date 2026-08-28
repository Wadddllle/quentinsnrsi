from pathlib import Path
import openmc.data

# Create a clean data library object
lib = openmc.data.DataLibrary()

# Automatically find all .h5 files in your current directory
h5_files = Path('.').glob('*.h5')

# Scan and register each file to the library
for h5_file in h5_files:
    print(f"Registering: {h5_file.name}")
    lib.register_file(h5_file)

# Export the fully synchronized XML file
lib.export_to_xml('cross_sections.xml')
print("Successfully generated a complete cross_sections.xml file!")
