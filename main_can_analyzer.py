import sys
import os
import csv
import threading
import time
from datetime import datetime
from queue import Queue, Empty

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget, QTableWidgetItem,
    QMenuBar, QAction, QFileDialog, QMessageBox, QStatusBar, QHeaderView,
    QDialog, QFormLayout, QSpinBox, QGridLayout
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QObject
from PyQt5.QtGui import QColor

import pyqtgraph as pg
import can
# import cantools # Bỏ comment nếu sử dụng DBC

# --- Cấu hình cơ bản ---
DEFAULT_INTERFACE = 'slcan' if os.name == 'nt' else 'socketcan' # 'slcan' cho Windows, 'socketcan' cho Linux
DEFAULT_CHANNEL = 'COM3' if os.name == 'nt' else 'can0'       # Thay đổi nếu cần
DEFAULT_BAUDRATE = 500000
SUPPORTED_BAUDRATES = [125000, 250000, 500000, 1000000]
PLOT_UPDATE_INTERVAL = 100 # ms - Tần suất cập nhật đồ thị
MAX_PLOT_POINTS = 200      # Số điểm tối đa trên đồ thị

# --- Worker đọc CAN chạy ngầm ---
class CanWorker(QObject):
    message_received = pyqtSignal(can.Message)
    error_occurred = pyqtSignal(str)
    _is_running = True
    _bus = None
    _message_queue = None # Sử dụng queue nội bộ thay vì truyền từ ngoài vào

    def __init__(self, interface_config):
        super().__init__()
        self.interface_config = interface_config
        self._message_queue = Queue() # Tạo queue khi khởi tạo worker

    def run(self):
        """Chạy vòng lặp đọc CAN."""
        self._is_running = True
        try:
            print(f"Attempting to connect: {self.interface_config}")
            self._bus = can.interface.Bus(**self.interface_config)
            print(f"Connected to {self._bus.channel_info}")

            # Đẩy các message nhận được vào queue nội bộ
            notifier = can.Notifier(self._bus, [self._message_listener])

            while self._is_running:
                # Vòng lặp chờ notifier hoặc tín hiệu dừng
                time.sleep(0.05) # Giảm tải CPU

        except can.CanError as e:
            print(f"CAN Error during connection/read: {e}")
            self.error_occurred.emit(f"CAN Error: {e}")
        except Exception as e:
            print(f"Unexpected error in CAN thread: {e}")
            self.error_occurred.emit(f"Error: {e}")
        finally:
            if hasattr(self, 'notifier') and self.notifier:
                 self.notifier.stop()
            if self._bus:
                try:
                    self._bus.shutdown()
                    print("CAN bus shutdown.")
                except Exception as e:
                    print(f"Error during CAN bus shutdown: {e}")
            self._is_running = False # Đảm bảo cờ được đặt lại

    def _message_listener(self, msg):
        """Callback được gọi bởi Notifier khi có message."""
        if self._is_running:
            self.message_received.emit(msg) # Phát tín hiệu trực tiếp

    def stop(self):
        """Dừng worker."""
        print("Stopping CAN worker...")
        self._is_running = False
        # Không cần join thread ở đây vì QThread sẽ quản lý

    def send_message(self, msg):
        """Gửi tin nhắn CAN."""
        if self._bus and self._is_running:
            try:
                self._bus.send(msg)
                # print(f"Sent: {msg}") # Debug
                return True
            except can.CanError as e:
                self.error_occurred.emit(f"Send Error: {e}")
                return False
        return False

# --- Hộp thoại Cài đặt ---
class SettingsDialog(QDialog):
    def __init__(self, current_settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Settings")
        self.current_settings = current_settings

        layout = QFormLayout(self)

        self.interface_combo = QComboBox()
        self.interface_combo.addItems(['socketcan', 'slcan', 'pcan', 'vector', 'virtual']) # Thêm các loại interface
        self.interface_combo.setCurrentText(current_settings.get('interface', DEFAULT_INTERFACE))

        self.channel_edit = QLineEdit(current_settings.get('channel', DEFAULT_CHANNEL))

        self.baudrate_combo = QComboBox()
        self.baudrate_combo.addItems([str(br) for br in SUPPORTED_BAUDRATES])
        self.baudrate_combo.setCurrentText(str(current_settings.get('bitrate', DEFAULT_BAUDRATE)))

        layout.addRow("Interface:", self.interface_combo)
        layout.addRow("Channel:", self.channel_edit)
        layout.addRow("Baudrate:", self.baudrate_combo)

        # Nút OK và Cancel
        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        button_layout.addStretch()
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        layout.addRow(button_layout)

        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def get_settings(self):
        """Trả về dictionary chứa cài đặt mới."""
        return {
            'interface': self.interface_combo.currentText(),
            'channel': self.channel_edit.text(),
            'bitrate': int(self.baudrate_combo.currentText())
            # Thêm các tham số khác nếu cần (fd, state, etc.)
        }

# --- Cửa sổ chính ---
class MainWindow(QMainWindow):
    send_request = pyqtSignal(can.Message) # Tín hiệu để yêu cầu gửi từ thread chính

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyCANalyzer")
        self.setGeometry(100, 100, 1000, 800) # Tăng chiều cao

        # --- Thuộc tính ---
        self.can_worker = None
        self.can_thread = None
        self.is_connected = False
        self.log_file = None
        self.csv_writer = None
        self.is_logging = False
        self.message_counter = 0
        self.plot_data_x = {} # Key: ID, Value: list of timestamps/counters
        self.plot_data_y = {} # Key: ID, Value: list of data values
        self.plot_curves = {} # Key: ID, Value: PlotDataItem
        self.can_settings = {
            'interface': DEFAULT_INTERFACE,
            'channel': DEFAULT_CHANNEL,
            'bitrate': DEFAULT_BAUDRATE,
            # 'fd': False # Thêm nếu cần CAN FD
        }
        # self.db = None # Cho DBC

        # --- Giao diện ---
        self._init_ui()
        self._connect_signals()

        # Timer để cập nhật GUI từ queue (nếu dùng queue) hoặc xử lý tác vụ định kỳ
        self.ui_update_timer = QTimer(self)
        self.ui_update_timer.timeout.connect(self.update_plots) # Chỉ cập nhật plot định kỳ
        # self.ui_update_timer.start(50) # Cập nhật 20 lần/giây

    def _init_ui(self):
        # --- Menu Bar ---
        menubar = self.menuBar()
        file_menu = menubar.addMenu('&File')
        settings_menu = menubar.addMenu('&Settings')
        help_menu = menubar.addMenu('&Help')

        # File Menu Actions
        self.start_log_action = QAction('&Start Logging...', self)
        self.stop_log_action = QAction('Stop Logging', self)
        self.stop_log_action.setEnabled(False)
        # export_action = QAction('&Export Data...', self) # TBD
        exit_action = QAction('&Exit', self)

        file_menu.addAction(self.start_log_action)
        file_menu.addAction(self.stop_log_action)
        # file_menu.addAction(export_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        # Settings Menu Actions
        self.connect_action = QAction('&Connect', self)
        self.disconnect_action = QAction('&Disconnect', self)
        self.disconnect_action.setEnabled(False)
        self.configure_action = QAction('&Configure CAN...', self)
        # load_dbc_action = QAction('Load &DBC File...', self) # TBD

        settings_menu.addAction(self.connect_action)
        settings_menu.addAction(self.disconnect_action)
        settings_menu.addAction(self.configure_action)
        # settings_menu.addAction(load_dbc_action)

        # Help Menu Actions
        about_action = QAction('&About', self)
        help_menu.addAction(about_action)

        # --- Central Widget & Layout ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Receive Area ---
        self.receive_table = QTableWidget()
        self.receive_table.setColumnCount(7) # Tăng cột cho Counter
        self.receive_table.setHorizontalHeaderLabels([
            "Timestamp", "ID", "Type", "DLC", "Data (Hex)", "Count", "Bus"
        ])
        self.receive_table.setEditTriggers(QTableWidget.NoEditTriggers) # Không cho sửa trực tiếp
        self.receive_table.verticalHeader().setVisible(False) # Ẩn header dọc
        header = self.receive_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents) # Timestamp
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents) # ID
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents) # Type
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents) # DLC
        header.setSectionResizeMode(4, QHeaderView.Stretch)         # Data
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents) # Count
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents) # Bus
        main_layout.addWidget(self.receive_table, stretch=2) # Chiếm nhiều không gian hơn

        # --- Send Area ---
        send_group = QGroupBox("Send CAN Message")
        send_layout = QGridLayout(send_group)

        send_layout.addWidget(QLabel("ID (Hex):"), 0, 0)
        self.send_id_edit = QLineEdit("123")
        send_layout.addWidget(self.send_id_edit, 0, 1)

        send_layout.addWidget(QLabel("Type:"), 0, 2)
        self.send_type_combo = QComboBox()
        self.send_type_combo.addItems(["Data Frame", "Remote Frame (RTR)"])
        send_layout.addWidget(self.send_type_combo, 0, 3)

        send_layout.addWidget(QLabel("Data (Hex):"), 1, 0)
        self.send_data_edit = QLineEdit("00 11 22 33 44 55 66 77")
        send_layout.addWidget(self.send_data_edit, 1, 1, 1, 3) # Span 3 cột

        # Tùy chọn gửi định kỳ (chưa triển khai logic gửi định kỳ)
        # send_layout.addWidget(QLabel("Send Rate (ms):"), 2, 0)
        # self.send_rate_spin = QSpinBox()
        # self.send_rate_spin.setRange(0, 10000) # 0 = send immediately
        # self.send_rate_spin.setValue(0)
        # send_layout.addWidget(self.send_rate_spin, 2, 1)

        self.send_button = QPushButton("Send")
        self.send_button.setEnabled(False) # Chỉ bật khi kết nối
        send_layout.addWidget(self.send_button, 2, 3) # Đặt nút Send ở cuối

        main_layout.addWidget(send_group)

        # --- Plot Area ---
        plot_group = QGroupBox("Real-time Plot")
        plot_layout = QVBoxLayout(plot_group)
        self.plot_widget = pg.PlotWidget()
        plot_layout.addWidget(self.plot_widget)
        main_layout.addWidget(plot_group, stretch=1) # Cho plot ít không gian hơn table

        # --- Status Bar ---
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Disconnected")

        # --- Plotting Setup ---
        self.plot_widget.setBackground('w')
        self.plot_widget.setLabel('left', 'Value')
        self.plot_widget.setLabel('bottom', 'Time (Sequence)')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()

    def _connect_signals(self):
        # Menu Actions
        self.connect_action.triggered.connect(self.connect_can)
        self.disconnect_action.triggered.connect(self.disconnect_can)
        self.configure_action.triggered.connect(self.configure_settings)
        self.start_log_action.triggered.connect(self.start_logging)
        self.stop_log_action.triggered.connect(self.stop_logging)
        self.menuBar().findChildren(QAction)[-2].triggered.connect(self.show_about) # About action
        self.menuBar().findChildren(QAction)[-4].triggered.connect(self.close) # Exit action

        # Send Button
        self.send_button.clicked.connect(self.prepare_send_message)

        # Tín hiệu từ Worker Thread
        # self.send_request đã được khai báo trong __init__

    def show_about(self):
        QMessageBox.about(self, "About PyCANalyzer",
                          "Simple CAN Bus Analyzer\n"
                          "Using PyQt5, python-can, pyqtgraph\n"
                          "Inspired by PCAN-View")

    # --- CAN Connection ---
    def connect_can(self):
        if self.is_connected:
            return

        # Tạo worker và thread mới mỗi lần kết nối
        # Điều này đảm bảo trạng thái sạch sẽ
        self.can_worker = CanWorker(self.can_settings)
        self.can_thread = QThread(self) # Parent là MainWindow để quản lý vòng đời

        # Di chuyển worker vào thread mới
        self.can_worker.moveToThread(self.can_thread)

        # Kết nối tín hiệu/slot GIỮA CÁC THREAD
        self.can_worker.message_received.connect(self.handle_message)
        self.can_worker.error_occurred.connect(self.handle_can_error)
        self.send_request.connect(self.can_worker.send_message) # Tín hiệu gửi từ main -> worker
        self.can_thread.started.connect(self.can_worker.run)
        self.can_thread.finished.connect(self.on_thread_finished) # Dọn dẹp khi thread kết thúc

        # Bắt đầu thread
        self.can_thread.start()

        # Cập nhật trạng thái GUI ngay lập tức (có thể hơi lạc quan)
        self.is_connected = True # Giả định thành công, sẽ sửa nếu có lỗi ngay
        self.update_connection_status()
        self.status_bar.showMessage(f"Connecting to {self.can_settings['interface']}:{self.can_settings['channel']} @ {self.can_settings['bitrate']} bps...")
        # Bật timer cập nhật plot
        self.ui_update_timer.start(PLOT_UPDATE_INTERVAL)


    def disconnect_can(self):
        if not self.is_connected or not self.can_thread or not self.can_thread.isRunning():
            print("Not connected or thread not running.")
            # Đảm bảo trạng thái GUI đúng ngay cả khi có lỗi trước đó
            self.is_connected = False
            self.update_connection_status()
            self.status_bar.showMessage("Disconnected")
            if self.ui_update_timer.isActive():
                self.ui_update_timer.stop()
            return

        print("Requesting CAN worker stop...")
        # Dừng worker một cách an toàn
        if self.can_worker:
            self.can_worker.stop()

        # Yêu cầu thread dừng và đợi nó kết thúc
        self.can_thread.quit()
        if not self.can_thread.wait(2000): # Đợi tối đa 2 giây
             print("Warning: CAN thread did not finish gracefully. Terminating.")
             self.can_thread.terminate() # Buộc dừng nếu không phản hồi

        # Đặt lại is_connected SAU KHI thread đã thực sự dừng
        # self.is_connected = False # Sẽ được đặt trong on_thread_finished

        # Cập nhật trạng thái GUI (sẽ được cập nhật thêm trong on_thread_finished)
        self.status_bar.showMessage("Disconnecting...")
        self.stop_logging() # Tự động dừng ghi log khi ngắt kết nối
        if self.ui_update_timer.isActive():
            self.ui_update_timer.stop()

    def on_thread_finished(self):
        """Được gọi khi QThread kết thúc."""
        print("CAN thread finished.")
        self.is_connected = False
        # Dọn dẹp tài nguyên worker và thread cũ
        # Việc xóa worker nên được thực hiện cẩn thận, đảm bảo không còn tham chiếu
        # self.can_worker.deleteLater() # Lên lịch xóa worker an toàn
        self.can_worker = None
        self.can_thread = None
        self.update_connection_status()
        self.status_bar.showMessage("Disconnected")
        # Dừng timer nếu chưa dừng
        if self.ui_update_timer.isActive():
            self.ui_update_timer.stop()


    def update_connection_status(self):
        """Cập nhật trạng thái các nút và menu dựa trên trạng thái kết nối."""
        self.connect_action.setEnabled(not self.is_connected)
        self.disconnect_action.setEnabled(self.is_connected)
        self.send_button.setEnabled(self.is_connected)
        self.configure_action.setEnabled(not self.is_connected) # Chỉ cấu hình khi ngắt kết nối

        if self.is_connected:
            rate_kbps = self.can_settings.get('bitrate', 0) // 1000
            status = f"Connected to {self.can_settings.get('channel', 'N/A')} @ {rate_kbps} kbps"
            self.status_bar.showMessage(status)
        # else: # Trạng thái Disconnected được xử lý trong disconnect_can và on_thread_finished
        #     self.status_bar.showMessage("Disconnected")


    # --- Settings ---
    def configure_settings(self):
        if self.is_connected:
            QMessageBox.warning(self, "Warning", "Please disconnect before changing settings.")
            return

        dialog = SettingsDialog(self.can_settings, self)
        if dialog.exec_() == QDialog.Accepted:
            self.can_settings = dialog.get_settings()
            print(f"New settings: {self.can_settings}")
            # Cập nhật status bar để hiển thị cài đặt mới (ngay cả khi chưa kết nối)
            rate_kbps = self.can_settings.get('bitrate', 0) // 1000
            self.status_bar.showMessage(f"Ready ({self.can_settings.get('channel', 'N/A')} @ {rate_kbps} kbps)")


    # --- Message Handling ---
    def handle_message(self, msg):
        """Xử lý tin nhắn CAN nhận được từ worker thread."""
        self.message_counter += 1
        timestamp_str = f"{msg.timestamp:.6f}" # Hoặc format theo datetime
        # timestamp_dt = datetime.fromtimestamp(msg.timestamp)
        # timestamp_str = timestamp_dt.strftime("%H:%M:%S.") + f"{timestamp_dt.microsecond // 1000:03d}"

        id_str = f"{msg.arbitration_id:X}" # Hex format
        if msg.is_extended_id:
            id_str += " (Ext)"
        else:
            id_str += " (Std)"

        if msg.is_remote_frame:
            msg_type = "Remote"
            data_str = "N/A"
        elif msg.is_error_frame:
            msg_type = "Error"
            data_str = f"Error Data: {msg.data.hex().upper()}" # Có thể không có data thực
        elif msg.is_fd: # CAN FD
             msg_type = f"FD {'BRS ' if msg.bitrate_switch else ''}"
             data_str = msg.data.hex(' ').upper()
        else: # Standard CAN Data Frame
            msg_type = "Data"
            data_str = msg.data.hex(' ').upper()

        dlc = msg.dlc
        channel_info = msg.channel if msg.channel else self.can_settings.get('channel', 'N/A')

        # --- Cập nhật bảng ---
        row_position = self.receive_table.rowCount()
        self.receive_table.insertRow(row_position)

        self.receive_table.setItem(row_position, 0, QTableWidgetItem(timestamp_str))
        self.receive_table.setItem(row_position, 1, QTableWidgetItem(id_str))
        self.receive_table.setItem(row_position, 2, QTableWidgetItem(msg_type))
        self.receive_table.setItem(row_position, 3, QTableWidgetItem(str(dlc)))
        self.receive_table.setItem(row_position, 4, QTableWidgetItem(data_str))
        self.receive_table.setItem(row_position, 5, QTableWidgetItem(str(self.message_counter)))
        self.receive_table.setItem(row_position, 6, QTableWidgetItem(str(channel_info)))

        # Tự cuộn xuống dòng mới nhất
        self.receive_table.scrollToBottom()

        # --- Ghi log ---
        if self.is_logging and self.csv_writer:
            try:
                self.csv_writer.writerow([
                    timestamp_str,
                    f"{msg.arbitration_id:X}",
                    "E" if msg.is_extended_id else "S",
                    msg_type,
                    dlc,
                    data_str.replace(" ", ""), # Ghi hex liền mạch
                    self.message_counter,
                    channel_info
                ])
            except Exception as e:
                print(f"Error writing to log file: {e}")
                self.handle_can_error(f"Log Write Error: {e}") # Thông báo lỗi lên status bar


        # --- Cập nhật dữ liệu cho đồ thị (thu thập dữ liệu) ---
        # Ví dụ: Chỉ vẽ byte đầu tiên của ID 0x18FF03EF
        target_id = 0x18FF03EF # Thay đổi ID bạn muốn vẽ
        if msg.arbitration_id == target_id and not msg.is_remote_frame and msg.dlc > 0:
             if target_id not in self.plot_data_x:
                 self.plot_data_x[target_id] = []
                 self.plot_data_y[target_id] = []
                 # Tạo đường cong mới nếu chưa có
                 pen_color = pg.mkPen(color=(len(self.plot_curves) % 9 * 30, len(self.plot_curves)*20 % 255, 255 - len(self.plot_curves)*10 % 255 ), width=2)
                 self.plot_curves[target_id] = self.plot_widget.plot(pen=pen_color, name=f"ID {target_id:X} - Byte 0")

             # Sử dụng bộ đếm làm trục X cho đơn giản
             self.plot_data_x[target_id].append(self.message_counter)
             self.plot_data_y[target_id].append(msg.data[0]) # Lấy byte đầu tiên

             # Giới hạn số điểm để tránh lag
             if len(self.plot_data_x[target_id]) > MAX_PLOT_POINTS:
                 self.plot_data_x[target_id].pop(0)
                 self.plot_data_y[target_id].pop(0)

        # Việc vẽ đồ thị thực tế sẽ được thực hiện trong self.update_plots bởi QTimer


    def handle_can_error(self, error_message):
        """Hiển thị lỗi CAN trên status bar và tùy chọn là MessageBox."""
        print(f"CAN Error reported: {error_message}")
        self.status_bar.showMessage(f"Error: {error_message}", 5000) # Hiển thị lỗi trong 5 giây
        # QMessageBox.critical(self, "CAN Error", error_message) # Bỏ comment nếu muốn hiện popup
        # Có thể xem xét ngắt kết nối tự động nếu lỗi nghiêm trọng
        if "No such device" in error_message or "Cannot find specified device" in error_message:
             print("Device not found error, attempting auto-disconnect.")
             self.disconnect_can() # Tự động ngắt kết nối nếu thiết bị không tồn tại


    def prepare_send_message(self):
        """Lấy thông tin từ GUI và yêu cầu gửi qua tín hiệu."""
        if not self.is_connected:
            QMessageBox.warning(self, "Not Connected", "Connect to CAN bus before sending.")
            return

        try:
            msg_id_str = self.send_id_edit.text()
            is_extended = '(Ext)' in msg_id_str # Tạm thời kiểm tra đơn giản
            msg_id = int(msg_id_str.replace('(Ext)','').replace('(Std)','').strip(), 16)

            is_remote = self.send_type_combo.currentIndex() == 1

            data_str = self.send_data_edit.text().strip()
            data_bytes = bytearray()
            dlc = 0
            if not is_remote:
                 if data_str:
                     hex_values = data_str.split()
                     data_bytes = bytearray.fromhex("".join(hex_values))
                     dlc = len(data_bytes)
                     if dlc > 8: # Giả sử là CAN cổ điển, chưa hỗ trợ FD gửi
                         raise ValueError("Data length exceeds 8 bytes for standard CAN.")
                 else:
                     dlc = 0 # Gửi frame dữ liệu với DLC=0
            else: # Remote Frame
                # DLC cho Remote Frame cần được xác định (thường là 0 hoặc chiều dài mong đợi)
                # Người dùng có thể cần nhập DLC riêng cho RTR
                dlc = 0 # Mặc định DLC=0 cho RTR, hoặc lấy từ một ô nhập khác

            message = can.Message(
                arbitration_id=msg_id,
                is_extended_id=is_extended,
                is_remote_frame=is_remote,
                dlc=dlc,
                data=data_bytes if not is_remote else None
            )

            # Phát tín hiệu yêu cầu gửi, worker sẽ thực hiện việc gửi
            self.send_request.emit(message)
            print(f"Requesting send: {message}") # Debug

            # (Tùy chọn) Thêm message đã gửi vào bảng hiển thị
            # self.add_sent_message_to_table(message)

        except ValueError as e:
            QMessageBox.critical(self, "Invalid Input", f"Error parsing send data: {e}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An unexpected error occurred during send preparation: {e}")


    # --- Logging ---
    def start_logging(self):
        if self.is_logging:
            return

        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        file_path, _ = QFileDialog.getSaveFileName(self, "Save CAN Log", "",
                                                   "CSV Files (*.csv);;All Files (*)", options=options)

        if file_path:
            try:
                # Đảm bảo phần mở rộng là .csv
                if not file_path.lower().endswith('.csv'):
                    file_path += '.csv'

                self.log_file = open(file_path, 'w', newline='', encoding='utf-8')
                self.csv_writer = csv.writer(self.log_file)
                # Viết header
                self.csv_writer.writerow([
                    "Timestamp", "ID (Hex)", "ID Type", "Msg Type", "DLC", "Data (Hex)", "Count", "Bus"
                ])
                self.is_logging = True
                self.start_log_action.setEnabled(False)
                self.stop_log_action.setEnabled(True)
                self.status_bar.showMessage(f"Logging started: {os.path.basename(file_path)}")
                print(f"Logging started to: {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Log Error", f"Could not start logging: {e}")
                if self.log_file:
                    self.log_file.close()
                self.log_file = None
                self.csv_writer = None
                self.is_logging = False

    def stop_logging(self):
        if not self.is_logging:
            return

        self.is_logging = False
        if self.log_file:
            try:
                self.log_file.close()
                print(f"Logging stopped: {self.log_file.name}")
                self.status_bar.showMessage(f"Logging stopped: {os.path.basename(self.log_file.name)}")
            except Exception as e:
                print(f"Error closing log file: {e}")
        self.log_file = None
        self.csv_writer = None
        self.start_log_action.setEnabled(True)
        self.stop_log_action.setEnabled(False)
        # Giữ thông điệp trạng thái kết nối nếu đang kết nối
        if self.is_connected:
            self.update_connection_status()
        else:
            # Cập nhật nếu không còn kết nối
             if not self.status_bar.currentMessage().startswith("Error"):
                  self.status_bar.showMessage("Disconnected")


    # --- Plotting ---
    def update_plots(self):
        """Cập nhật các đường cong trên đồ thị với dữ liệu đã thu thập."""
        if not self.isVisible(): # Không cập nhật nếu cửa sổ bị ẩn
             return

        for target_id, curve in self.plot_curves.items():
            if target_id in self.plot_data_x and target_id in self.plot_data_y:
                x_data = self.plot_data_x[target_id]
                y_data = self.plot_data_y[target_id]
                if x_data and y_data: # Chỉ cập nhật nếu có dữ liệu
                    curve.setData(x=list(x_data), y=list(y_data)) # Chuyển sang list để pyqtgraph xử lý

    # --- Cleanup ---
    def closeEvent(self, event):
        """Đảm bảo ngắt kết nối và dừng thread khi đóng ứng dụng."""
        print("Close event triggered.")
        self.stop_logging() # Dừng ghi log nếu đang chạy
        if self.is_connected:
            self.disconnect_can()
            # Có thể cần đợi thread dừng hoàn toàn ở đây nếu disconnect_can không đồng bộ
            if self.can_thread and self.can_thread.isRunning():
                 print("Waiting for CAN thread to finish before closing...")
                 self.can_thread.wait(1000) # Đợi thêm 1 giây

        event.accept() # Chấp nhận sự kiện đóng


if __name__ == '__main__':
    # Bật chế độ DPI cao nếu có
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec_())
