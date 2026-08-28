import pandas as pd
import plotly.express as px

csv_file = "clean_deposition_points.csv"
print(f"Reading {csv_file}...")
df = pd.read_csv(csv_file)

print(f"Plotting {len(df)} points in your web browser...")

# Create a clean 3D interactive scatter plot
fig = px.scatter_3d(
    df, 
    x='X_Coord', 
    y='Y_Coord', 
    z='Z_Coord',
    title="Ansys Fluent - Massless Particle Deposition Cloud",
    opacity=0.8
)

# Make the dots look sharp and clean
fig.update_traces(marker=dict(size=3, color='red'))

# Change the background grid theme to dark so the points pop
fig.update_layout(template="plotly_dark")

# Force it to open your Windows browser automatically
fig.show()
