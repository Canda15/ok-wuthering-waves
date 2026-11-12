from ok.device.interaction_methods.foreground_input import ForegroundInputInterrupted


class WWOneTimeTask:

    def run(self):
        # 启动等待前先清除上次中断的战斗状态；前台准备由执行器统一完成。
        if hasattr(self, 'reset_combat_state'):
            self.reset_combat_state('onetime task start')
        while True:
            try:
                self.sleep(.5)
                return
            except ForegroundInputInterrupted:
                # 只恢复启动等待，不重新调用具体任务 run 或执行激活补救。
                self.wait_foreground_frame()
