import ttkbootstrap as tb
import tkinter.ttk as ttk

app = tb.Window(themename="superhero")
app.title("TTKBootstrap OK")

ttk.Label(app, text="Si ves tema oscuro, está OK ✅").pack(padx=20, pady=20)
ttk.Button(app, text="Botón").pack(padx=20, pady=10)

app.mainloop()
