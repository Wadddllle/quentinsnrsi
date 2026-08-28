import re

# Replace with the path to your renamed text file
input_file_path = "particles_final.txt"
output_file_path = "clean_deposition_points.csv"

print("Cleaning Ansys export file...")

with open(input_file_path, "r") as f_in, open(output_file_path, "w") as f_out:
    # Write a clean header for ParaView
    f_out.write("Particle_ID,X_Coord,Y_Coord,Z_Coord\n")
    
    for line in f_in:
        cleaned_line = line.strip()
        
        # Skip EnSight header strings
        if not cleaned_line or any(x in cleaned_line for x in ["FORMAT", "type", "GEOMETRY", "model", "measured", "VARIABLE", "TIME", "particle coordinates"]):
            continue
            
        # Use regex to find rows starting with a particle ID number followed by scientific notation coordinates
        # Example line matched: "2 2.94765e+04 2.94550e+04 1.00585e+03"
        match = re.match(r"^(\d+)\s+([\d\.eE\+\-]+)\s+([\d\.eE\+\-]+)\s+([\d\.eE\+\-]+)$", cleaned_line)
        
        if match:
            # Extract data and join with commas
            particle_id, x, y, z = match.groups()
            f_out.write(f"{particle_id},{x},{y},{z}\n")

print(f"Success! Clean data saved to: {output_file_path}")
